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
    # Redis: очередь RQ (и result backend Celery, если не задан result_backend_url)
    redis_url: str = "redis://localhost:6379/1"
    # Отдельный Redis под result backend Celery и метрики бенчмарка.
    # Роли разные: брокер — транспорт, backend — хранилище. В эксперименте это ещё и условие
    # честного сравнения: иначе в конфигурации «Celery + Redis» один сервер обслуживает и то, и другое.
    result_backend_url: str = ""
    # События для Flower: удобно в демо, но это лишние сообщения в брокер на каждую задачу.
    # В бенчмарке выключаются, иначе конфигурации с Redis и RabbitMQ нагружены по-разному.
    celery_events: bool = True
    # Publisher confirms для RabbitMQ: публикация ждёт подтверждения брокера.
    # Выключено по умолчанию (умолчание Celery), включается для сравнения при равных гарантиях.
    broker_confirm_publish: bool = False
    # Каталог артефактов отчётов (общий volume между api и воркерами)
    artifacts_dir: Path = Path("./artifacts")
    # Вероятность сбоя задачи уведомления (демонстрация retry)
    notify_failure_rate: float = 0.5
    # Класс воркера RQ: fork — процесс на задачу (изоляция), simple — в том же процессе (быстрее)
    rq_worker_class: Literal["fork", "simple"] = "fork"

    @property
    def result_backend(self) -> str:
        """Куда Celery кладёт результаты и куда бенчмарк пишет метрики."""
        return self.result_backend_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
