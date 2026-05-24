"""Общая логика: адреса, заглушки, checkpoint JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SKIP_PLACEHOLDERS = frozenset(
    {
        "нету на сайте",
        "нет на сайте",
        "н/д",
        "нд",
    }
)


def is_skippable_address(text: str) -> bool:
    t = text.strip().lower()
    if not t or t == "адрес":
        return True
    if t in SKIP_PLACEHOLDERS:
        return True
    if "нету на сайте" in t or "нет на сайте" in t:
        return True
    return False


def load_addresses(path: Path) -> tuple[list[str], list[str]]:
    addresses: list[str] = []
    skipped: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        if is_skippable_address(text):
            skipped.append(text)
            continue
        addresses.append(text)
    return addresses, skipped


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_checkpoint(output_path: Path, source: str) -> dict:
    if not output_path.exists():
        return {
            "источник": source,
            "создано": now_iso(),
            "обновлено": now_iso(),
            "пропущено_заглушек": 0,
            "обработано": 0,
            "результаты": [],
        }
    data = json.loads(output_path.read_text(encoding="utf-8"))
    if "результаты" not in data:
        data["результаты"] = []
    data.setdefault("источник", source)
    data.setdefault("создано", now_iso())
    return data


def processed_addresses(checkpoint: dict) -> set[str]:
    return {r["адрес"] for r in checkpoint.get("результаты", []) if r.get("адрес")}


def pending_addresses(all_addresses: list[str], checkpoint: dict) -> list[str]:
    done = processed_addresses(checkpoint)
    return [a for a in all_addresses if a not in done]


def save_checkpoint(
    output_path: Path,
    checkpoint: dict,
    *,
    skipped_placeholders: int = 0,
) -> None:
    checkpoint["обновлено"] = now_iso()
    checkpoint["обработано"] = len(checkpoint.get("результаты", []))
    if skipped_placeholders:
        checkpoint["пропущено_заглушек"] = skipped_placeholders
    output_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_result(
    output_path: Path,
    checkpoint: dict,
    result_entry: dict,
    *,
    skipped_placeholders: int = 0,
) -> None:
    checkpoint["результаты"].append(result_entry)
    save_checkpoint(output_path, checkpoint, skipped_placeholders=skipped_placeholders)
