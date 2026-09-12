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
CONFIG_ORDER = ["celery-redis", "celery-rabbitmq", "rq-redis", "rq-simple-redis"]
COLORS = {"celery-redis": "#2a78d6", "celery-rabbitmq": "#eb6834", "rq-redis": "#1baf7a", "rq-simple-redis": "#eda100"}
LABELS = {
    "celery-redis": "Celery + Redis",
    "celery-rabbitmq": "Celery + RabbitMQ",
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
    """Пропускная способность vs число воркеров — по одному полю на вид задачи (N=1000)."""
    sub = df[df["n"] == 1000]
    kinds = [k for k in ("noop", "cpu_small", "io_sleep") if k in set(sub["kind"])]
    fig, axes = plt.subplots(1, len(kinds), figsize=(4.6 * len(kinds), 4), sharex=True, squeeze=False)
    axes = axes[0]
    for ax, kind in zip(axes, kinds):
        data = sub[sub["kind"] == kind]
        for config, g in data.groupby("config", observed=True):
            g = g.sort_values("workers")
            ax.plot(g["workers"], g["throughput"], marker="o", lw=2, ms=6, color=COLORS[config])
            last = g.iloc[-1]
            ax.annotate(f"{last['throughput']:.0f}", (last["workers"], last["throughput"]),
                        textcoords="offset points", xytext=(6, 0), fontsize=8, color="#52514e", va="center")
        ax.set_title(KIND_TITLE[kind], fontsize=10)
        ax.set_xlabel("воркеров")
        ax.set_xticks([1, 2, 4])
        ax.set_ylim(bottom=0)
    axes[0].set_ylabel("задач / с")
    _legend(axes[0], list(sub["config"].cat.categories))
    fig.suptitle("Пропускная способность (N = 1000 задач, медиана 3 повторов)", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(RESULTS / "throughput.png", dpi=150)
    plt.close(fig)


def plot_latency(df: pd.DataFrame) -> None:
    """noop, N=1000: p50 и p95 задержки enqueue→finish. Это очередь + накладные расходы, не «скорость Redis»."""
    data = df[(df["kind"] == "noop") & (df["n"] == 1000)]
    configs = list(data["config"].cat.categories)
    workers = sorted(data["workers"].unique())
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    width = 0.8 / len(configs)
    for ax, metric, title in zip(axes, ("latency_p50_ms", "latency_p95_ms"), ("p50", "p95")):
        for i, config in enumerate(configs):
            g = data[data["config"] == config].set_index("workers").reindex(workers)
            x = [w_i + (i - (len(configs) - 1) / 2) * width for w_i in range(len(workers))]
            bars = ax.bar(x, g[metric], width=width * 0.92, color=COLORS[config], label=LABELS[config])
            for b, v in zip(bars, g[metric]):
                if pd.notna(v):
                    ax.annotate(f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}", (b.get_x() + b.get_width() / 2, v),
                                textcoords="offset points", xytext=(0, 2), ha="center", fontsize=7, color="#52514e")
        ax.set_xticks(range(len(workers)), [f"{w} воркер{'а' if w > 1 else ''}" for w in workers])
        ax.set_title(f"Задержка {title}, мс (log)", fontsize=10)
        ax.set_yscale("log")
    axes[0].legend(fontsize=8, loc="upper right")
    fig.suptitle("noop, N = 1000: задержка от постановки до завершения", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
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
    sub = df[df["n"] == 1000]
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
    fig.suptitle("Горизонтальное масштабирование (N = 1000)", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(RESULTS / "scaling.png", dpi=150)
    plt.close(fig)


def write_markdown(df: pd.DataFrame) -> None:
    """Таблицы для docs/experiment.md."""
    lines = []
    for kind in ("noop", "cpu_small", "io_sleep"):
        for n in sorted(df[df["kind"] == kind]["n"].unique()):
            data = df[(df["kind"] == kind) & (df["n"] == n)]
            lines.append(f"### {kind}, N = {n}\n")
            lines.append("| Конфигурация | Воркеров | Постановка, msg/s | Всего, с | Пропускная, задач/с | p50, мс | p95, мс | p99, мс |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for _, r in data.iterrows():
                lines.append(f"| {LABELS[r['config']]} | {r['workers']} | {r['enqueue_rate']:.0f} | {r['total_sec']:.1f} | "
                             f"**{r['throughput']:.0f}** | {r['latency_p50_ms']:.0f} | {r['latency_p95_ms']:.0f} | {r['latency_p99_ms']:.0f} |")
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
