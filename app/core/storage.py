"""Хранилище артефактов: файловая система, общий volume между API и воркерами.

Почему файлы, а не payload в сообщении: брокер — не хранилище. Сообщение
несёт только report_id и параметры, результат (сотни КБ) лежит на диске,
в result backend — лишь имена файлов.
"""

from pathlib import Path

from app.core.config import get_settings


def report_dir(report_id: str, *, create: bool = False) -> Path:
    """Каталог артефактов одного отчёта: ARTIFACTS_DIR/<report_id>/."""
    path = get_settings().artifacts_dir / report_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(report_id: str, filename: str) -> Path | None:
    """Путь к файлу артефакта, если он существует (без выхода за каталог отчёта)."""
    base = report_dir(report_id).resolve()
    path = (base / filename).resolve()
    if path.parent != base or not path.is_file():
        return None
    return path
