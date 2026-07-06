"""Тесты FormattingMixin — календарь, счёт дней до старта, блоки контекста.

Пиннят фиксы инцидентов с датами (off-by-one «завтра старт» 02.07.2026):
арифметику дат считает код, Claude получает готовые слова.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.analyst import HealthAnalyst  # noqa: E402

TODAY = date(2026, 7, 6)  # понедельник


class FormattingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = HealthAnalyst(api_key="test-key", model="test-model", user_age=37)


class TestRaceCountdown(FormattingTestCase):
    def test_words_not_arithmetic(self):
        # регресс «завтра старт» (02.07.2026): готовое слово вместо «[2 дней]»
        self.assertEqual(self.a._race_countdown("2026-07-06", TODAY), "понедельник, СЕГОДНЯ")
        self.assertEqual(self.a._race_countdown("2026-07-07", TODAY), "вторник, завтра")
        self.assertEqual(self.a._race_countdown("2026-07-08", TODAY), "среда, послезавтра")
        self.assertEqual(self.a._race_countdown("2026-07-12", TODAY), "воскресенье, через 6 дн.")

    def test_far_race_includes_weeks(self):
        out = self.a._race_countdown("2026-09-27", TODAY)
        self.assertIn("воскресенье", out)
        self.assertIn("нед.", out)


class TestCalendarBlock(FormattingTestCase):
    def test_today_and_tomorrow_marked(self):
        block = self.a._calendar_block("2026-07-06")
        lines = block.splitlines()
        self.assertIn("не вычисляй самостоятельно", lines[0])
        self.assertIn("Понедельник 06.07", lines[1])
        self.assertIn("← Сегодня", lines[1])
        self.assertIn("Вторник 07.07", lines[2])
        self.assertIn("← Завтра", lines[2])
        self.assertEqual(len(lines), 9)  # заголовок + 8 дней


class TestMetricsBlocks(FormattingTestCase):
    def test_race_block_precomputed(self):
        ctx = self.a._format_metrics({
            "date": "2026-07-06",
            "upcoming_races": [
                {"date": "2026-07-08", "name": "Ночной старт", "distance_km": 10.0},
            ],
        })
        self.assertIn("сам не пересчитывай", ctx)
        self.assertIn("[до старта: среда, послезавтра]", ctx)

    def test_metrics_light_has_calendar(self):
        ctx = self.a._format_metrics_light({"date": "2026-07-06"})
        self.assertIn("КАЛЕНДАРЬ", ctx)
        self.assertIn("Понедельник 06.07", ctx)

    def test_format_header(self):
        self.assertIn("2026-07-06", self.a.format_header({"date": "2026-07-06"}))


if __name__ == "__main__":
    unittest.main()
