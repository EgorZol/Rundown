"""Фасад GarminService = синк (garmin_sync) + чтение метрик (garmin_metrics).

Публичный API не менялся: bot.py / plan_builder / main импортируют только
GarminService. Здесь живут __init__ и общие хелперы (конверсии времени,
поиск per-user БД), нужные обоим миксинам.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .garmin_metrics import GarminMetricsMixin, SyncSummary  # noqa: F401  (реэкспорт)
from .garmin_sync import BackupResult, GarminSyncMixin, _mask_email  # noqa: F401  (реэкспорт)

logger = logging.getLogger(__name__)


class GarminService(GarminSyncMixin, GarminMetricsMixin):
    def __init__(self, workdir_root: Path, exports_dir: Path) -> None:
        self._workdir_root = workdir_root
        self._exports_dir = exports_dir
        self._workdir_root.mkdir(parents=True, exist_ok=True)
        self._exports_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _secs_to_time_str(secs: float | None) -> str | None:
        if secs is None:
            return None
        total_secs = int(round(secs))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _secs_to_time_str_precise(secs: float | None) -> str | None:
        if secs is None:
            return None
        total_secs = int(secs)
        ms = int(round((secs - total_secs) * 1000))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}000"

    @staticmethod
    def _secs_to_pace_str(secs: float | None) -> str | None:
        if secs is None or secs <= 0:
            return None
        total_secs = int(secs)
        ms = int(round((secs - total_secs) * 1000000))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:06d}"

    def _garmin_db_path(self, user_id: int, db_name: str) -> Path | None:
        path = self._workdir_root / str(user_id) / "DBs" / db_name
        return path if path.exists() else None

    def _analytics_db_path_for_user(self, user_id: int) -> Path:
        return self._workdir_root / str(user_id) / "CoachData" / "coach_metrics.db"

    def _find_db_with_table(self, dbs_dir: Path, table_name: str) -> Path | None:
        for db in sorted(dbs_dir.glob("*.db")):
            with sqlite3.connect(db, timeout=5) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (table_name,),
                ).fetchone()
            if row:
                return db
        return None

    def _find_dbs_dir(self, output_dir: Path) -> Path | None:
        # Search a few levels deep for a plausible DBs directory.
        # We keep it shallow to avoid scanning huge trees.
        candidates: list[Path] = []
        for depth in (1, 2, 3, 4, 5):
            for p in output_dir.glob("*/" * (depth - 1) + "DBs"):
                if p.is_dir():
                    candidates.append(p)
        for p in sorted(candidates, key=lambda x: len(str(x))):
            if any(p.glob("*.db")):
                return p
        return candidates[0] if candidates else None
