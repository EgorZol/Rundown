"""Подключение умных весов: разбор OAuth-кода и записей Zepp.

Ключевое (грабли 21.07.2026): поле muscleRate содержит КИЛОГРАММЫ, хотя
называется «Rate». Проверяется инвариантой мышцы+жир+кости = вес.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot import zepp_scale  # noqa: E402

REAL = {  # реальная запись с весов Егора, 21.07.2026
    "weight": 89.3, "height": 183.0, "bmi": 26.6, "fatRate": 26.618885,
    "bodyWaterRate": 50.339443, "boneMass": 3.3410983, "metabolism": 1744.0,
    "muscleRate": 62.18824, "muscleAge": 36, "proteinRatio": 19.300236,
    "visceralFat": 12.0, "impedance": 412, "bodyScore": 59,
}


class TestExtractCode(unittest.TestCase):
    def test_full_redirect_url(self):
        url = ("https://api-mifit-cn.huami.com/huami.health.loginview.do"
               "?code=KSMOSCLOUDSRV_E9B31072FD840B2D&state=x")
        self.assertEqual(zepp_scale.extract_code(url), "KSMOSCLOUDSRV_E9B31072FD840B2D")

    def test_bare_code(self):
        code = "KSMOSCLOUDSRV_E9B31072FD840B2D02BD03B5"
        self.assertEqual(zepp_scale.extract_code(code), code)

    def test_garbage(self):
        for bad in ("", "   ", "привет", "не знаю что это"):
            self.assertIsNone(zepp_scale.extract_code(bad))

    def test_url_without_code(self):
        self.assertIsNone(zepp_scale.extract_code("https://example.com/?foo=1"))


class TestParseRecord(unittest.TestCase):
    def _parse(self, summary):
        ts = int(time.mktime((2026, 7, 21, 11, 44, 0, 0, 0, -1)))
        return zepp_scale._parse_record({"summary": summary, "weightType": 0}, ts)

    def test_muscle_is_kilograms_not_percent(self):
        rec = self._parse(REAL)
        # 62.19 — это КГ; процент считаем сами и он должен быть ~69.6
        self.assertAlmostEqual(rec["muscle_kg"], 62.18824, places=3)
        self.assertAlmostEqual(rec["muscle_pct"], 69.64, delta=0.1)

    def test_components_sum_to_weight(self):
        rec = self._parse(REAL)
        self.assertTrue(zepp_scale.composition_is_consistent(rec))

    def test_inconsistent_record_detected(self):
        # то, что получилось бы, прочитай мы muscleRate как процент
        broken = dict(self._parse(REAL))
        broken["muscle_kg"] = 89.3 * 62.18824 / 100  # 55.5 кг
        self.assertFalse(zepp_scale.composition_is_consistent(broken))

    def test_measurement_without_impedance(self):
        # весы не сняли состав тела — только вес
        rec = self._parse({"weight": 87.0, "bmi": 25.9})
        self.assertEqual(rec["weight"], 87.0)
        self.assertIsNone(rec["fat_pct"])
        self.assertIsNone(rec["muscle_kg"])
        self.assertTrue(zepp_scale.composition_is_consistent(rec))

    def test_zero_fat_is_treated_as_missing(self):
        rec = self._parse(dict(REAL, fatRate=0))
        self.assertIsNone(rec["fat_pct"])

    def test_no_weight_skipped(self):
        self.assertIsNone(self._parse({"bmi": 25.0}))


class TestAuthorizeUrl(unittest.TestCase):
    def test_contains_client_and_redirect(self):
        url = zepp_scale.authorize_url()
        self.assertIn(zepp_scale.CLIENT_ID, url)
        self.assertIn("response_type=code", url)
        self.assertIn("api-mifit-cn.huami.com", url)


if __name__ == "__main__":
    unittest.main()
