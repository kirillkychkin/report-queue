"""Синтетические данные о продажах.

Внешних источников нет намеренно: демо должно работать офлайн и быть
воспроизводимым (один seed → один и тот же отчёт), что делает задачу
идемпотентной — важное свойство при at-least-once доставке.
"""

from datetime import date

import numpy as np
import pandas as pd

REGIONS = ["Москва", "Санкт-Петербург", "Новосибирск", "Якутск", "Казань", "Владивосток"]
CATEGORIES = ["Электроника", "Одежда", "Продукты", "Книги", "Спорт"]


def generate_sales(rows: int, date_from: date, date_to: date, seed: int) -> pd.DataFrame:
    """Таблица продаж: date, region, category, quantity, price, amount."""
    rng = np.random.default_rng(seed)
    days = pd.date_range(date_from, date_to, freq="D")

    dates = rng.choice(days, size=rows)
    regions = rng.choice(REGIONS, size=rows, p=_region_weights())
    categories = rng.choice(CATEGORIES, size=rows)
    quantity = rng.integers(1, 10, size=rows)
    # Цена зависит от категории + лог-нормальный шум — чтобы графики были не плоскими
    base_price = np.array([_BASE_PRICE[c] for c in categories])
    price = np.round(base_price * rng.lognormal(mean=0.0, sigma=0.3, size=rows), 2)

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "region": regions,
            "category": categories,
            "quantity": quantity,
            "price": price,
        }
    )
    df["amount"] = (df["quantity"] * df["price"]).round(2)
    return df.sort_values("date", ignore_index=True)


_BASE_PRICE = {"Электроника": 15_000, "Одежда": 3_000, "Продукты": 500, "Книги": 800, "Спорт": 4_500}


def _region_weights() -> list[float]:
    # Неравномерное распределение — крупные города продают больше
    w = np.array([0.30, 0.22, 0.13, 0.08, 0.15, 0.12])
    return list(w / w.sum())
