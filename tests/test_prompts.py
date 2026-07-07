"""Тесты карты возможностей (prompts.CAPABILITIES) — единый источник истины об UI.

Главная гарантия: каждая кнопка BTN_* из bot.py описана в карте, и наоборот —
в карте нет фантомных кнопок. Инциденты-первопричины: бот выдумывал
несуществующие пути UI («кнопка синхронизации», «удаление в 🍽 Еда»),
потому что знание об интерфейсе было размазано по промптам и неполно.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot import bot as botmod  # noqa: E402
from garmin_backup_bot import prompts  # noqa: E402

REAL_BUTTONS = {v for k, v in vars(botmod).items() if k.startswith("BTN_")}


class TestCapabilityMapComplete(unittest.TestCase):
    def test_every_real_button_is_described(self):
        missing = REAL_BUTTONS - set(prompts.CAPABILITIES)
        self.assertFalse(
            missing,
            f"Кнопки без описания в prompts.CAPABILITIES: {missing}. "
            "Добавил кнопку — опиши её в карте возможностей.",
        )

    def test_no_phantom_buttons_in_map(self):
        phantom = set(prompts.CAPABILITIES) - REAL_BUTTONS
        self.assertFalse(
            phantom,
            f"В карте описаны несуществующие кнопки: {phantom}. "
            "Удалил/переименовал кнопку — обнови карту.",
        )

    def test_hidden_flags_match_keyboard(self):
        # кнопки, реально размещённые на MAIN_KEYBOARD
        on_keyboard = {
            btn.text
            for row in botmod.MAIN_KEYBOARD.keyboard
            for btn in row
        }
        for name, cap in prompts.CAPABILITIES.items():
            if cap.get("hidden"):
                self.assertNotIn(name, on_keyboard,
                                 f"«{name}» помечена hidden, но есть на клавиатуре")
            else:
                self.assertIn(name, on_keyboard,
                              f"«{name}» не помечена hidden, но на клавиатуре её нет")


class TestRenders(unittest.TestCase):
    def test_prompt_block_mentions_every_button(self):
        block = prompts.capabilities_prompt_block()
        for name in prompts.CAPABILITIES:
            self.assertIn(name, block)
        self.assertIn("НЕВОЗМОЖНО", block)

    def test_help_shows_visible_hides_hidden(self):
        h = prompts.help_text()
        for name, cap in prompts.CAPABILITIES.items():
            if cap.get("hidden"):
                self.assertNotIn(f"{name} —", h)
            else:
                self.assertIn(name, h)

    def test_ask_prompt_contains_map_and_race_tools(self):
        sp = prompts.build_ask_stable_prompt("123")
        for marker in ("🗺 КАРТА UI", "add_race", "delete_race", "set_race_priority"):
            self.assertIn(marker, sp)


if __name__ == "__main__":
    unittest.main()
