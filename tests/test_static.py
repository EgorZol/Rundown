"""Статическая проверка: в коде нет обращений к неопределённым именам.

Инцидент 19.07.2026: `BTN_PLAN` использовался в bot_reports без импорта —
NameError всплывал только в рантайме, у юзера падала кнопка План.
Юнит-тесты этого не ловят (NameError живёт внутри редких веток хендлеров),
а pyflakes находит мгновенно.
"""
import glob
import io
import os
import unittest

from pyflakes.api import checkPath
from pyflakes.reporter import Reporter

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "garmin_backup_bot")


class TestNoUndefinedNames(unittest.TestCase):
    def test_pyflakes_undefined_names(self):
        out = io.StringIO()
        reporter = Reporter(out, out)
        for path in sorted(glob.glob(os.path.join(SRC, "*.py"))):
            checkPath(path, reporter)
        problems = [
            line
            for line in out.getvalue().splitlines()
            if "undefined name" in line or "syntax" in line.lower()
        ]
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
