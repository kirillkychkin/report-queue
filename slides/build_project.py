"""Сборка презентации проекта: slides/content/project.md -> slides/project.pptx.

Запуск с хоста: ``.venv/Scripts/python slides/build_project.py``
"""

from __future__ import annotations

from pathlib import Path

from builder import build_deck

HERE = Path(__file__).resolve().parent


def main() -> None:
    output = build_deck(HERE / "content" / "project.md", HERE / "project.pptx")
    print(f"готово: {output}")


if __name__ == "__main__":
    main()
