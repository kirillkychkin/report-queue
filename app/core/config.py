"""Настройки приложения из переменных окружения (.env)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Какой фреймворк фоновых задач использует API
    task_backend: Literal["celery", "rq"] = "celery"
    # Брокер Celery: redis://... или amqp://...
    broker_url: str = "redis://localhost:6379/0"
    # Redis: result backend Celery и очередь RQ
    redis_url: str = "redis://localhost:6379/1"
    # Каталог артефактов отчётов (общий volume между api и воркерами)
    artifacts_dir: Path = Path("./artifacts")
    # Вероятность сбоя задачи уведомления (демонстрация retry)
    notify_failure_rate: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
