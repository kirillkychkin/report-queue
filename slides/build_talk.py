"""Сборка презентации доклада: slides/content/talk.md -> slides/talk.pptx.

Запуск с хоста: ``.venv/Scripts/python slides/build_talk.py``
"""

from __future__ import annotations

from pathlib import Path

from builder import build_deck

HERE = Path(__file__).resolve().parent


def main() -> None:
    output = build_deck(HERE / "content" / "talk.md", HERE / "talk.pptx")
    print(f"готово: {output}")


if __name__ == "__main__":
    main()
