"""Тесты доменной логики отчётов (без брокеров и воркеров)."""

import time
from datetime import date

import pandas as pd
import pytest

from app.core.models import ReportParams
from app.core.storage import artifact_path
from app.reports.aggregate import aggregate_sales, monthly_trend
from app.reports.data import CATEGORIES, REGIONS, generate_sales
from app.reports.service import STEPS, generate_report


def test_generate_sales_shape_and_reproducibility():
    a = generate_sales(500, date(2025, 1, 1), date(2025, 3, 31), seed=1)
    b = generate_sales(500, date(2025, 1, 1), date(2025, 3, 31), seed=1)
    c = generate_sales(500, date(2025, 1, 1), date(2025, 3, 31), seed=2)

    assert list(a.columns) == ["date", "region", "category", "quantity", "price", "amount"]
    assert len(a) == 500
    assert set(a["region"]) <= set(REGIONS)
    assert set(a["category"]) <= set(CATEGORIES)
    assert a["date"].min() >= pd.Timestamp("2025-01-01")
    assert a["date"].max() <= pd.Timestamp("2025-03-31")
    assert (a["amount"] > 0).all()
    pd.testing.assert_frame_equal(a, b)  # одинаковый seed — одинаковые данные
    assert not a.equals(c)


@pytest.mark.parametrize("group_by", ["region", "category", "month"])
def test_aggregate_sales_preserves_total(group_by):
    df = generate_sales(2_000, date(2025, 1, 1), date(2025, 6, 30), seed=7)
    summary = aggregate_sales(df, group_by)

    assert list(summary.columns) == [group_by, "orders", "quantity", "amount", "avg_check"]
    assert summary["orders"].sum() == len(df)
    assert summary["amount"].sum() == pytest.approx(df["amount"].sum(), abs=0.05 * len(summary))
    if group_by == "month":
        assert summary["month"].is_monotonic_increasing
        assert len(summary) == 6


def test_monthly_trend_covers_every_month():
    df = generate_sales(3_000, date(2025, 1, 1), date(2025, 12, 31), seed=3)
    trend = monthly_trend(df)
    assert len(trend) == 12
    assert trend.sum() == pytest.approx(df["amount"].sum(), abs=1.0)


def test_generate_report_creates_artifacts_and_reports_progress(artifacts_tmp):
    params = ReportParams(rows=1_000, group_by="category")
    progress: list[tuple[int, str]] = []

    result = generate_report("r-1", params, progress_cb=lambda p, s: progress.append((p, s)))

    assert result.report_id == "r-1"
    assert result.rows == 1_000
    assert result.total_amount > 0
    assert set(result.artifacts) == {"csv", "png", "pdf"}
    for filename in result.artifacts.values():
        path = artifact_path("r-1", filename)
        assert path is not None and path.stat().st_size > 0
    assert (artifacts_tmp / "r-1").is_dir()

    assert progress == [(pct, step) for step, pct in STEPS]  # все шаги в порядке
    assert progress[-1][0] == 100


def test_generate_report_is_idempotent():
    params = ReportParams(rows=500, seed=11)
    first = generate_report("same-id", params)
    csv_first = artifact_path("same-id", first.artifacts["csv"]).read_bytes()

    second = generate_report("same-id", params)
    csv_second = artifact_path("same-id", second.artifacts["csv"]).read_bytes()

    assert csv_first == csv_second
    assert first.total_amount == second.total_amount


def test_artifact_path_rejects_traversal(artifacts_tmp):
    generate_report("safe", ReportParams(rows=200))
    assert artifact_path("safe", "../summary.csv") is None
    assert artifact_path("..", "safe/summary.csv") is None
    assert artifact_path("safe", "missing.csv") is None


def test_report_10k_rows_is_fast_enough():
    started = time.perf_counter()
    generate_report("perf", ReportParams(rows=10_000, group_by="month"))
    assert time.perf_counter() - started < 3.0


def test_report_params_validation():
    with pytest.raises(ValueError):
        ReportParams(rows=10)  # меньше минимума
    with pytest.raises(ValueError):
        ReportParams(group_by="year")  # неизвестное измерение
