"""Сначала kadastor, затем geoinf portal."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main() -> int:
    py = sys.executable
    scripts = [
        BASE / "parser_kadastor.py",
        BASE / "parser_geoinf_portal.py",
    ]
    for script in scripts:
        print(f"\n{'=' * 60}\nЗапуск: {script.name}\n{'=' * 60}\n", flush=True)
        rc = subprocess.call([py, str(script)], cwd=str(BASE.parent))
        if rc != 0:
            print(f"Ошибка {script.name}, код {rc}", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
