"""Домен «память»: заметки user_memory (remember/forget/список, теги [ЗАПОМНИТЬ]).
"""


import logging
import re
from datetime import date, timedelta

from telegram import (
    Update,
)
from telegram.ext import ContextTypes

from .bot_common import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


def _parse_expiry(text: str, today: date | None = None) -> str | None:
    """Парсит срок жизни заметки. Возвращает ISO-дату или None.

    Принимает:
      • ISO  — 2026-07-05
      • DD.MM[.YYYY] / DD-MM[-YYYY] / DD/MM[/YYYY]
      • относительные: «завтра», «послезавтра», «через N дней/дн.»,
        «через N недель/нед.», «через месяц»
    """
    if not text:
        return None
    today = today or date.today()
    s = text.strip().lower()
    # ISO
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    # DD.MM[.YYYY] (а также с / и -)
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", s)
    if m:
        d, mon = int(m.group(1)), int(m.group(2))
        year_part = m.group(3)
        if year_part:
            year = int(year_part)
            if year < 100:
                year += 2000
        else:
            year = today.year
            # если дата уже в прошлом — берём следующий год
            try:
                candidate = date(year, mon, d)
                if candidate < today:
                    year += 1
            except ValueError:
                return None
        try:
            return date(year, mon, d).isoformat()
        except ValueError:
            return None
    if s in ("завтра",):
        return (today + timedelta(days=1)).isoformat()
    if s in ("послезавтра",):
        return (today + timedelta(days=2)).isoformat()
    m = re.fullmatch(r"через\s+(\d+)\s*(дн|день|дня|дней|нед|недел[ьюяи]+|месяц[аев]*)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("нед"):
            return (today + timedelta(weeks=n)).isoformat()
        if unit.startswith("месяц"):
            return (today + timedelta(days=30 * n)).isoformat()
        return (today + timedelta(days=n)).isoformat()
    if s in ("через месяц",):
        return (today + timedelta(days=30)).isoformat()
    return None


_BAD_MEMORY_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\bцел[ьи][\s—:\-—]", "это похоже на цель — просто скажи мне словами «моя цель — …»"),
    (r"(?i)\bплан\s+(?:на|недел|трениров)", "это похоже на план — используй кнопку 📅 План"),
    (r"(?i)\b(?:марафон|полумарафон|забег|старт|гонка)\b.{0,40}\d{1,2}[./\-]\d{1,2}",
     "гонка с датой — скажи мне «добавь забег <название> <дата>»"),
    (r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}", "конкретная дата — скажи мне словами что это (гонка, цель и т.п.)"),
    (r"(?i)\bLTHR\s*[:=]?\s*\d{2,3}\b", "LTHR — просто скажи «мой порог 172», сохраню в профиль"),
    (r"(?i)\bвес\s*[:=]?\s*\d{2,3}(?:[.,]\d)?\s*кг", "вес — просто скажи «мой вес 72.5», сохраню в профиль"),
    (r"(?i)\bчасовой\s+пояс\b", "часовой пояс — используй кнопку 🕐 Часы"),
]


def _classify_bad_memory(note: str) -> str | None:
    """Возвращает причину отказа сохранять заметку или None если ок."""
    text = (note or "").strip()
    if not text:
        return "пустая строка"
    if len(text) > 300:
        return "слишком длинная (>300 симв.) — обычно это план/отчёт, не заметка"
    for pattern, reason in _BAD_MEMORY_PATTERNS:
        if re.search(pattern, text):
            return reason
    return None


class MemoryMixin:

    async def remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Save persistent notes that Claude always sees.

        Usage:
          /remember <text>
          /remember --until 2026-07-05 <text>   ← с датой истечения
          /remember --until 05.07 <text>
          /remember --until завтра <text>
        """
        user_id = update.effective_user.id
        args = list(context.args or [])
        expires_at: str | None = None
        if args and args[0].lower() == "--until" and len(args) >= 2:
            raw_date = args[1]
            parsed = _parse_expiry(raw_date)
            if not parsed:
                await update.message.reply_text(
                    f"Не понял дату «{raw_date}». Примеры: 2026-07-05, 05.07, завтра, через 14 дней",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            expires_at = parsed
            args = args[2:]
        note = " ".join(args).strip()
        if not note:
            await update.message.reply_text(
                "💬 Проще всего — просто скажи мне обычным текстом:\n"
                "  «не переношу жару»\n"
                "  «бегаю утром, не вечером»\n"
                "  «у меня антибиотики 14 дней»\n"
                "  «болит колено, исключи прыжки»\n\n"
                "Я сам пойму намерение и сохраню.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        bad_reason = _classify_bad_memory(note)
        if bad_reason:
            await update.message.reply_text(
                f"Не сохранил — {bad_reason}",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        new_id = self._storage.add_memory_item(user_id, note, expires_at=expires_at)
        if new_id is None:
            await update.message.reply_text(
                "Уже есть похожая заметка — не дублирую.", reply_markup=MAIN_KEYBOARD
            )
            return
        suffix = f" (до {expires_at})" if expires_at else ""
        await self._show_memory_list(update, user_id, header=f"Запомнил (#{new_id}){suffix}.")

    async def show_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Show current persistent memory as a numbered list."""
        user_id = update.effective_user.id
        await self._show_memory_list(update, user_id)

    async def _show_memory_list(
        self, update: Update, user_id: int, header: str | None = None
    ) -> None:
        items = self._storage.list_memory_items(user_id)
        if not items:
            await update.message.reply_text(
                "Заметок пока нет.\n\n"
                "💬 Просто скажи мне обычным текстом — я запомню:\n"
                "  «не переношу жару»\n"
                "  «болит колено, исключи прыжки»\n"
                "  «у меня антибиотики 14 дней» (сам поставлю срок)",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        lines = []
        if header:
            lines.append(header)
            lines.append("")
        lines.append("Твои заметки (видны боту всегда):")
        for it in items:
            exp = f" ⏳ до {it['expires_at']}" if it.get("expires_at") else ""
            lines.append(f"#{it['id']}. {it['content']}{exp}")
        lines.append("")
        lines.append("💬 Чтобы убрать — просто скажи: «забудь про X» или «забудь #N».")
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)

    async def forget_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Удалить заметку. Power-user команда; обычный способ — сказать словами."""
        user_id = update.effective_user.id
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "💬 Просто скажи мне словами: «забудь про антибиотики», «забудь #3», «забудь всё».",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        token = args[0].strip().lower()
        if token in ("all", "все", "всё"):
            n = self._storage.clear_user_memory(user_id)
            await update.message.reply_text(
                f"Удалил все заметки ({n} шт.).", reply_markup=MAIN_KEYBOARD
            )
            return
        try:
            item_id = int(token.lstrip("#"))
        except ValueError:
            await update.message.reply_text(
                "Не понял номер. Скажи словами «забудь про X» — я найду.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if self._storage.delete_memory_item(user_id, item_id):
            await self._show_memory_list(update, user_id, header=f"Удалил #{item_id}.")
        else:
            await update.message.reply_text(
                f"Заметку #{item_id} не нашёл (возможно уже удалена).",
                reply_markup=MAIN_KEYBOARD,
            )

    @staticmethod
    def _extract_memories(text: str) -> tuple[str, list[tuple[str, str | None]]]:
        """Extract [ЗАПОМНИТЬ[ до DATE]: ...] tags. Return (clean_text, [(content, expires_iso_or_None)…]).

        Поддерживает:
          [ЗАПОМНИТЬ: текст]                  — бессрочно
          [ЗАПОМНИТЬ до 2026-07-05: текст]    — ISO
          [ЗАПОМНИТЬ до 05.07: текст]         — DD.MM (год авто)
          [ЗАПОМНИТЬ до завтра: текст]        — relative
        """
        memories: list[tuple[str, str | None]] = []
        clean = text
        pattern = re.compile(r'\[ЗАПОМНИТЬ(?:\s+до\s+([^\]:]+?))?:\s*(.+?)\]')
        for m in pattern.finditer(text):
            raw_expiry = (m.group(1) or "").strip()
            content = m.group(2).strip()
            expires_at = _parse_expiry(raw_expiry) if raw_expiry else None
            memories.append((content, expires_at))
            clean = clean.replace(m.group(0), "")
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
        return clean, memories

    @staticmethod
    def _strip_memory_tags(text: str) -> str:
        """Тихо удалить любые [ЗАПОМНИТЬ:…] — для путей вывода, где
        авто-память не должна обрабатываться (план, анализ, итог недели)."""
        if "[ЗАПОМНИТЬ" not in text:
            return text
        cleaned = re.sub(r"\[ЗАПОМНИТЬ:\s*.+?\]", "", text)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()
