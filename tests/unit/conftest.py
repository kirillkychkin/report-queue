"""Общие фикстуры unit-тестов: артефакты пишутся во временный каталог."""

import pytest

from app.core import config


@pytest.fixture(autouse=True)
def artifacts_tmp(tmp_path, monkeypatch):
    """Каждый тест получает свой ARTIFACTS_DIR и чистый кэш настроек."""
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    config.get_settings.cache_clear()
    yield tmp_path / "artifacts"
    config.get_settings.cache_clear()
