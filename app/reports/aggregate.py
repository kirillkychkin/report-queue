"""Агрегация продаж по выбранному измерению."""

import pandas as pd

from app.core.models import GroupBy


def aggregate_sales(df: pd.DataFrame, group_by: GroupBy) -> pd.DataFrame:
    """Сводная таблица: измерение → количество заказов, штук, сумма, средний чек.

    Для `month` группируем по началу месяца, чтобы ось времени была упорядочена.
    """
    if group_by == "month":
        key = df["date"].dt.to_period("M").dt.to_timestamp()
        key.name = "month"
    else:
        key = df[group_by]

    summary = (
        df.groupby(key, observed=True)
        .agg(
            orders=("amount", "size"),
            quantity=("quantity", "sum"),
            amount=("amount", "sum"),
            avg_check=("amount", "mean"),
        )
        .round(2)
    )
    if group_by != "month":
        summary = summary.sort_values("amount", ascending=False)
    return summary.reset_index()


def monthly_trend(df: pd.DataFrame) -> pd.Series:
    """Сумма продаж по месяцам — для линейного графика динамики."""
    return df.set_index("date")["amount"].resample("MS").sum().round(2)
