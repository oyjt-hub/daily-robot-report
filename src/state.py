from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from .models import Paper


class SeenPaperStore:
    """Atomic JSON store tracking processed arXiv IDs to skip holiday duplicates."""

    def __init__(self, file_path: str | Path, retention_days: int = 90) -> None:
        self.file_path = Path(file_path)
        self.retention_days = retention_days
        self._records: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            self._records = {}
            return
        try:
            with self.file_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._records = {str(k): str(v) for k, v in data.items()}
            else:
                self._records = {}
        except Exception:
            self._records = {}

    def prune(self, reference_date: datetime.date | None = None) -> int:
        if self.retention_days <= 0:
            return 0
        ref = reference_date or datetime.date.today()
        cutoff = ref - datetime.timedelta(days=self.retention_days)
        removed = 0
        kept: dict[str, str] = {}
        for pid, date_str in self._records.items():
            try:
                seen_date = datetime.date.fromisoformat(date_str[:10])
                if seen_date >= cutoff:
                    kept[pid] = date_str
                else:
                    removed += 1
            except ValueError:
                kept[pid] = date_str
        self._records = kept
        return removed

    def is_seen(self, paper_id: str) -> bool:
        return paper_id in self._records

    def filter_unseen(self, papers: list[Paper]) -> list[Paper]:
        return [p for p in papers if p.id not in self._records]

    def mark_seen(self, paper_ids: list[str], date_iso: str) -> None:
        for pid in paper_ids:
            if pid:
                self._records[pid] = date_iso

    def save(self) -> None:
        self.prune()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._records, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, self.file_path)

    def __len__(self) -> int:
        return len(self._records)
