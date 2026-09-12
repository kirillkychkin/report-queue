"""Абстракция TaskBackend: нормализация состояний Celery и RQ, фабрика, фейковый бэкенд."""

import pytest

from app.core.models import ReportParams, ReportState, ReportStatus
from app.tasks import backend as b


class FakeBackend:
    """Минимальная реализация протокола — то, что API увидит от любого бэкенда."""

    name = "fake"

    def __init__(self) -> None:
        self.jobs: dict[str, ReportStatus] = {}

    def enqueue_report(self, report_id: str, params: ReportParams) -> str:
        self.jobs[report_id] = ReportStatus(id=report_id, state=ReportState.PENDING)
        return report_id

    def get_status(self, report_id: str) -> ReportStatus:
        if report_id not in self.jobs:
            raise b.UnknownReport(report_id)
        return self.jobs[report_id]

    def ping(self) -> bool:
        return True


def test_fake_backend_satisfies_protocol():
    backend: b.TaskBackend = FakeBackend()
    assert backend.enqueue_report("r", ReportParams()) == "r"
    assert backend.get_status("r").state == ReportState.PENDING
    with pytest.raises(b.UnknownReport):
        backend.get_status("missing")


# --- Celery: состояние + info → ReportStatus ---


@pytest.mark.parametrize(
    "state, info, expected",
    [
        ("PENDING", None, (ReportState.PENDING, 0, None)),
        ("STARTED", None, (ReportState.STARTED, 0, None)),
        ("RETRY", Exception("x"), (ReportState.STARTED, 0, None)),
        ("PROGRESS", {"progress": 55, "step": "render_csv"}, (ReportState.PROGRESS, 55, "render_csv")),
    ],
)
def test_status_from_celery_intermediate(state, info, expected):
    status = b.status_from_celery("id", state, info)
    assert (status.state, status.progress, status.step) == expected
    assert status.artifacts == {}


def test_status_from_celery_terminal():
    ok = b.status_from_celery("id", "SUCCESS", {"artifacts": {"csv": "summary.csv"}, "rows": 10})
    assert ok.state == ReportState.SUCCESS and ok.progress == 100 and ok.artifacts == {"csv": "summary.csv"}

    failed = b.status_from_celery("id", "FAILURE", ValueError("boom"))
    assert failed.state == ReportState.FAILURE and "boom" in failed.error


# --- RQ: статус job + meta + результат → ReportStatus ---


@pytest.mark.parametrize(
    "status, meta, expected",
    [
        ("queued", {"progress": 0}, (ReportState.PENDING, 0, None)),
        ("deferred", None, (ReportState.PENDING, 0, None)),
        ("scheduled", None, (ReportState.PENDING, 0, None)),
        ("started", {"progress": 0, "step": None}, (ReportState.STARTED, 0, None)),
        ("started", {"progress": 75, "step": "render_png"}, (ReportState.PROGRESS, 75, "render_png")),
    ],
)
def test_status_from_rq_intermediate(status, meta, expected):
    result = b.status_from_rq("id", status, meta, None, None)
    assert (result.state, result.progress, result.step) == expected


def test_status_from_rq_terminal():
    ok = b.status_from_rq("id", "finished", {}, {"artifacts": {"pdf": "report.pdf"}}, None)
    assert ok.state == ReportState.SUCCESS and ok.artifacts == {"pdf": "report.pdf"}

    failed = b.status_from_rq("id", "failed", {}, None, "Traceback...\nValueError: poison\n")
    assert failed.state == ReportState.FAILURE and failed.error == "ValueError: poison"

    stopped = b.status_from_rq("id", "stopped", {}, None, None)
    assert stopped.state == ReportState.FAILURE and stopped.error == "stopped"


def test_status_from_rq_accepts_enum_like_status():
    class JobStatus:
        value = "finished"

    assert b.status_from_rq("id", JobStatus(), {}, {"artifacts": {}}, None).state == ReportState.SUCCESS


# --- фабрика ---


def test_get_backend_by_name_returns_matching_implementation(monkeypatch):
    monkeypatch.setattr(b, "_BACKENDS", {"fake": FakeBackend})
    assert b.get_backend("fake").name == "fake"
    with pytest.raises(ValueError, match="unknown TASK_BACKEND"):
        b.get_backend("celery")


def test_get_backend_uses_settings_default(monkeypatch):
    monkeypatch.setattr(b, "_BACKENDS", {"rq": FakeBackend})
    monkeypatch.setattr(b, "get_settings", lambda: type("S", (), {"task_backend": "rq"})())
    assert isinstance(b.get_backend(), FakeBackend)
