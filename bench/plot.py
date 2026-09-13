"""Графики и markdown-таблицы по results/summary.csv.

    .venv/Scripts/python -m bench.plot

Выход: results/throughput.png, latency.png, enqueue.png, scaling.png, summary.md
Графики статичные (для PPTX и docs), поэтому подписи и легенда — прямо на рисунке.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

RESULTS = Path(__file__).parent / "results"
SUMMARY = RESULTS / "summary.csv"

# Цвет закреплён за конфигурацией (не за позицией в легенде)
CONFIG_ORDER = ["celery-redis", "celery-rabbitmq", "celery-rabbitmq-confirm", "rq-redis", "rq-simple-redis"]
COLORS = {
    "celery-redis": "#2a78d6",
    "celery-rabbitmq": "#eb6834",
    "celery-rabbitmq-confirm": "#a2452a",
    "rq-redis": "#1baf7a",
    "rq-simple-redis": "#eda100",
}
LABELS = {
    "celery-redis": "Celery + Redis",
    "celery-rabbitmq": "Celery + RabbitMQ",
    "celery-rabbitmq-confirm": "Celery + RabbitMQ (confirms)",
    "rq-redis": "RQ (fork) + Redis",
    "rq-simple-redis": "RQ (SimpleWorker) + Redis",
}
KIND_TITLE = {"noop": "noop — накладные расходы очереди", "cpu_small": "cpu_small — ≈60 мс CPU", "io_sleep": "io_sleep — 50 мс ожидания"}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.6, "axes.titleweight": "bold",
    "figure.facecolor": "white", "axes.facecolor": "white", "legend.frameon": False,
})


def load() -> pd.DataFrame:
    df = pd.read_csv(SUMMARY)
    df["config"] = pd.Categorical(df["config"], [c for c in CONFIG_ORDER if c in set(df["config"])], ordered=True)
    return df.sort_values(["config", "kind", "n", "workers"])


def _legend(ax, configs):
    handles = [plt.Line2D([], [], color=COLORS[c], marker="o", lw=2, label=LABELS[c]) for c in configs]
    ax.legend(handles=handles, loc="upper left", fontsize=9)


def plot_throughput(df: pd.DataFrame) -> None:
    """Пропускная способность vs число воркеров — по одному полю на вид задачи.

    Очередь наполняется заранее, поэтому это скорость разбора очереди, а не скорость продьюсера.
    """
    sub = df
    kinds = [k for k in ("noop", "cpu_small", "io_sleep") if k in set(sub["kind"])]
    fig, axes = plt.subplots(1, len(kinds), figsize=(4.6 * len(kinds), 4), sharex=True, squeeze=False)
    axes = axes[0]
    for ax, kind in zip(axes, kinds):
        data = sub[sub["kind"] == kind]
        placed: list[float] = []
        span = data["throughput"].max() or 1
        for config, g in data.groupby("config", observed=True):
            g = g.sort_values("workers")
            ax.plot(g["workers"], g["throughput"], marker="o", lw=2, ms=6, color=COLORS[config])
            last = g.iloc[-1]
            # подпись только если не сливается с уже поставленной (ближе 5 % диапазона)
            if all(abs(last["throughput"] - y) > 0.05 * span for y in placed):
                ax.annotate(f"{last['throughput']:.0f}", (last["workers"], last["throughput"]),
                            textcoords="offset points", xytext=(6, 0), fontsize=8, color="#52514e", va="center")
                placed.append(last["throughput"])
        ax.set_title(KIND_TITLE[kind], fontsize=10)
        ax.set_xlabel("воркеров")
        ax.set_xticks([1, 2, 4])
        ax.set_ylim(bottom=0)
    axes[0].set_ylabel("задач / с")
    _legend(axes[0], list(sub["config"].cat.categories))
    fig.suptitle("Пропускная способность: разбор заранее наполненной очереди (медиана 3 повторов)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(RESULTS / "throughput.png", dpi=150)
    plt.close(fig)


def plot_latency(df: pd.DataFrame) -> None:
    """Накладные расходы на одну задачу: пустая очередь, задачи по одной, с ожиданием каждой.

    Это НЕ время ожидания в очереди (в v1 измерялось именно оно и подчинялось закону Литтла),
    а цена прохода одной задачи через очередь: сериализация → брокер → воркер → результат.
    """
    data = df[df["kind"] == "noop"].groupby("config", observed=True)[["latency_p50_ms", "latency_p95_ms"]].median()
    configs = list(data.index)
    fig, ax = plt.subplots(figsize=(9, 4))
    width = 0.38
    for i, (metric, title) in enumerate((("latency_p50_ms", "p50"), ("latency_p95_ms", "p95"))):
        x = [j + (i - 0.5) * width for j in range(len(configs))]
        bars = ax.bar(x, data[metric], width=width * 0.92,
                      color=[COLORS[c] for c in configs], alpha=1.0 if i == 0 else 0.55)
        for b, v in zip(bars, data[metric]):
            ax.annotate(f"{v:.1f}", (b.get_x() + b.get_width() / 2, v), textcoords="offset points",
                        xytext=(0, 2), ha="center", fontsize=8, color="#52514e")
    ax.set_xticks(range(len(configs)), [LABELS[c].replace(" + ", "\n+ ") for c in configs], fontsize=9)
    ax.set_ylabel("мс на задачу (log)")
    ax.set_yscale("log")
    ticks = [1, 2, 5, 10, 20, 50, 100]
    ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_ylim(1, 160)
    ax.set_title("Накладные расходы очереди: одна задача на пустой очереди\n"
                 "(слева p50, справа p95 — по 30 замеров)", fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(RESULTS / "latency.png", dpi=150)
    plt.close(fig)


def plot_enqueue(df: pd.DataFrame) -> None:
    """Скорость постановки задач продьюсером (не зависит от воркеров) — среднее по ячейкам noop."""
    data = df[df["kind"] == "noop"].groupby("config", observed=True)["enqueue_rate"].median()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    bars = ax.barh([LABELS[c] for c in data.index], data.values, color=[COLORS[c] for c in data.index], height=0.6)
    for b, v in zip(bars, data.values):
        ax.annotate(f"{v:.0f} msg/s", (v, b.get_y() + b.get_height() / 2), textcoords="offset points",
                    xytext=(4, 0), va="center", fontsize=9, color="#52514e")
    ax.invert_yaxis()
    ax.set_xlabel("сообщений / с (один продьюсер, последовательно)")
    ax.set_title("Скорость постановки задач в очередь", fontsize=12)
    ax.set_xlim(right=data.max() * 1.25)
    fig.tight_layout()
    fig.savefig(RESULTS / "enqueue.png", dpi=150)
    plt.close(fig)


def plot_scaling(df: pd.DataFrame) -> None:
    """Масштабирование: пропускная способность относительно одного воркера (идеал — линия y = x)."""
    sub = df
    kinds = [k for k in ("noop", "cpu_small", "io_sleep") if k in set(sub["kind"])]
    fig, axes = plt.subplots(1, len(kinds), figsize=(4.6 * len(kinds), 4), sharey=True, squeeze=False)
    axes = axes[0]
    for ax, kind in zip(axes, kinds):
        data = sub[sub["kind"] == kind]
        ax.plot([1, 4], [1, 4], ls="--", color="#c3c2b7", lw=1, label="идеал")
        for config, g in data.groupby("config", observed=True):
            g = g.sort_values("workers")
            base = g[g["workers"] == 1]["throughput"]
            if base.empty:
                continue
            ax.plot(g["workers"], g["throughput"] / base.iloc[0], marker="o", lw=2, ms=6, color=COLORS[config])
        ax.set_title(KIND_TITLE[kind], fontsize=10)
        ax.set_xticks([1, 2, 4])
        ax.set_xlabel("воркеров")
    axes[0].set_ylabel("ускорение относительно 1 воркера")
    _legend(axes[0], list(sub["config"].cat.categories))
    fig.suptitle("Горизонтальное масштабирование (пропускная способность относительно 1 воркера)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(RESULTS / "scaling.png", dpi=150)
    plt.close(fig)


def write_markdown(df: pd.DataFrame) -> None:
    """Таблицы для docs/experiment.md."""
    lines = []
    for kind in ("noop", "cpu_small", "io_sleep"):
        data = df[df["kind"] == kind]
        if data.empty:
            continue
        lines.append(f"### {kind}\n")
        lines.append("| Конфигурация | N | Воркеров | Постановка, msg/s | Разбор очереди, с | Пропускная, задач/с | "
                     "Задержка p50, мс | p95, мс | p99, мс |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for _, r in data.iterrows():
            lines.append(f"| {LABELS[r['config']]} | {r['n']:.0f} | {r['workers']} | {r['enqueue_rate']:.0f} | "
                         f"{r['drain_sec']:.1f} | **{r['throughput']:.0f}** | {r['latency_p50_ms']:.2f} | "
                         f"{r['latency_p95_ms']:.2f} | {r['latency_p99_ms']:.2f} |")
        lines.append("")
    (RESULTS / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    df = load()
    plot_throughput(df)
    plot_latency(df)
    plot_enqueue(df)
    plot_scaling(df)
    write_markdown(df)
    print("written:", ", ".join(p.name for p in sorted(RESULTS.glob("*.png"))), "summary.md")


if __name__ == "__main__":
    main()
