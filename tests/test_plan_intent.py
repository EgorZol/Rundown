"""Детект «твик плана» vs «разговор о еде» (инцидент Алины 11.07:
сообщение о питании после бега перегенерировало план недели)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.bot_reports import ReportsMixin  # noqa: E402


class TestPlanIntent(unittest.TestCase):
    m = ReportsMixin()

    def test_alina_food_message_is_not_tweak(self):
        text = ("Сильно больше ем В день через два часа после интенсивного бега, "
                "вечером. Организм испытал стресс .\nБольше на хлеб, сыр, колбаса. "
                "На белок больше.  Сладкое нет.")
        self.assertFalse(self.m._is_plan_tweak(text))
        self.assertFalse(self.m._is_plan_request(text))

    def test_food_questions_not_plan(self):
        for t in ("сколько ккал я сжёг вчера?",
                  "хочу больше белка после бега",
                  "перебор по калориям в дни интервалов"):
            self.assertFalse(self.m._is_plan_tweak(t), t)
            self.assertFalse(self.m._is_plan_request(t), t)

    def test_real_tweaks_still_detected(self):
        for t in ("сделай план полегче",
                  "убери интервалы из плана на неделю",
                  "хочу больше длинных в плане"):
            self.assertTrue(self.m._is_plan_tweak(t), t)

    def test_real_requests_still_detected(self):
        for t in ("составь план на неделю", "дай новый план"):
            self.assertTrue(self.m._is_plan_request(t), t)


if __name__ == "__main__":
    unittest.main()
