"""Состав тела с умных весов: рендер строки для тренера.

Данные приходят из Zepp через scaleconnect/sync_body.py в таблицу
body_composition. Ключевое: fat_pct=0 — брак измерения (весы не сняли
импеданс), такие записи показывать нельзя.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.formatting import FormattingMixin  # noqa: E402

FULL = {
    "day": "2026-07-21", "weight": 89.3, "fat_pct": 26.618885,
    "muscle_kg": 55.53, "muscle_pct": 62.18824, "water_pct": 50.339443,
    "visceral_fat": 12.0, "bmr_kcal": 1744.0,
}


class TestBodyCompositionLine(unittest.TestCase):
    def test_full_record(self):
        line = FormattingMixin._body_composition_line({"body_composition": FULL})
        self.assertIn("2026-07-21", line)
        self.assertIn("жир 26.6%", line)
        self.assertIn("мышцы 55.5 кг", line)
        self.assertIn("вода 50.3%", line)
        self.assertIn("BMR 1744 ккал", line)

    def test_no_scale_data(self):
        self.assertIsNone(FormattingMixin._body_composition_line({}))
        self.assertIsNone(
            FormattingMixin._body_composition_line({"body_composition": None}))

    def test_broken_measurement_hidden(self):
        # весы не сняли импеданс — fat_pct=0, показывать нечего
        broken = dict(FULL, fat_pct=0.0)
        self.assertIsNone(
            FormattingMixin._body_composition_line({"body_composition": broken}))

    def test_partial_record(self):
        # старые записи содержат только вес и жир — не падаем
        partial = {"day": "2020-03-07", "weight": 86.3, "fat_pct": 25.0}
        line = FormattingMixin._body_composition_line({"body_composition": partial})
        self.assertIn("жир 25.0%", line)
        self.assertNotIn("мышцы", line)


if __name__ == "__main__":
    unittest.main()
