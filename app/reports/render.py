"""Рендер артефактов отчёта: CSV, PNG (график), PDF (многостраничный).

matplotlib работает с backend Agg — без дисплея, безопасно в воркере.
Фигуры закрываются явно: воркер долгоживущий, утечка памяти недопустима.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from app.core.models import GroupBy  # noqa: E402

FILENAMES = {"csv": "summary.csv", "png": "chart.png", "pdf": "report.pdf"}


def render_csv(summary: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / FILENAMES["csv"]
    summary.to_csv(path, index=False, encoding="utf-8-sig")  # BOM — чтобы Excel открыл кириллицу
    return path


def render_png(summary: pd.DataFrame, group_by: GroupBy, title: str, out_dir: Path) -> Path:
    path = out_dir / FILENAMES["png"]
    fig = _bar_chart(summary, group_by, title)
    try:
        fig.savefig(path, dpi=120, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path


def render_pdf(
    summary: pd.DataFrame, trend: pd.Series, group_by: GroupBy, title: str, out_dir: Path
) -> Path:
    path = out_dir / FILENAMES["pdf"]
    figures = [_bar_chart(summary, group_by, title), _trend_chart(trend, title), _table_page(summary, title)]
    try:
        with PdfPages(path) as pdf:
            for fig in figures:
                pdf.savefig(fig, bbox_inches="tight")
    finally:
        for fig in figures:
            plt.close(fig)
    return path


# --- построение фигур ---


def _bar_chart(summary: pd.DataFrame, group_by: GroupBy, title: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9, 5))
    labels = summary[group_by]
    if group_by == "month":
        labels = labels.dt.strftime("%Y-%m")
    ax.bar(labels, summary["amount"] / 1_000, color="#4C72B0")
    ax.set_title(f"{title}: сумма продаж по {_DIM_LABEL[group_by]}")
    ax.set_ylabel("тыс. руб.")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(axis="y", alpha=0.3)
    return fig


def _trend_chart(trend: pd.Series, title: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(trend.index, trend.values / 1_000, marker="o", color="#DD8452")
    ax.set_title(f"{title}: динамика по месяцам")
    ax.set_ylabel("тыс. руб.")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    return fig


def _table_page(summary: pd.DataFrame, title: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9, 0.4 * len(summary) + 1.5))
    ax.axis("off")
    ax.set_title(f"{title}: сводная таблица")
    display = summary.copy()
    for col in display.select_dtypes("datetime").columns:
        display[col] = display[col].dt.strftime("%Y-%m")
    table = ax.table(cellText=display.values, colLabels=list(display.columns), loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.3)
    return fig


_DIM_LABEL = {"region": "регионам", "category": "категориям", "month": "месяцам"}
