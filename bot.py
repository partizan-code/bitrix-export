"""
Одна кнопка «▶️ Запустить выгрузку»:
  - запускает sync.py подпроцессом;
  - повторный запуск во время работы запрещён (lock-файл с PID);
  - раз в 30 сек обновляет сообщение с прогрессом
    (читает data/progress.json, который пишет sync.py);
  - по завершении присылает ZIP-архив снимка (если <= ~45 МБ,
    иначе — путь на сервере).
"""

import asyncio
import glob
import json
import logging
import os
import subprocess
import sys
import time

import config  # загружает .env и даёт пути DATA_DIR/EXPORT_DIR

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("TG_CHAT_ID", "")
           .replace(" ", "").split(",") if x}

BASE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(config.DATA_DIR, "sync.lock")
PROGRESS = os.path.join(config.DATA_DIR, "progress.json")
RUN_LOG = os.path.join(config.DATA_DIR, "sync_run.log")

KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="▶️ Запустить выгрузку", callback_data="run")]])

MAX_TG_FILE = 45 * 1024 * 1024   # лимит Bot API ~50 МБ, берём с запасом
POLL_SEC = 5                    # период обновления прогресса в чате


# ------------------------------------------------------------------ helpers
def allowed(chat_id: int) -> bool:
    return bool(ALLOWED) and chat_id in ALLOWED


def running_pid():
    """PID живого sync.py из lock-файла, иначе None."""
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)          # жив ли процесс
        return pid
    except Exception:
        return None


def read_progress():
    try:
        with open(PROGRESS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def latest_zip():
    zips = glob.glob(os.path.join(config.EXPORT_DIR, "*.zip"))
    return max(zips, key=os.path.getmtime) if zips else None


def fmt_progress(p):
    """Текст сообщения о прогрессе из progress.json."""
    if not p:
        return "⏳ Выгрузка запущена, подготовка (справочники, выборка)…"
    ent = p.get("entity")
    if ent:
        title = "Сделки" if ent == "deal" else "Лиды"
        return (f"⚙️ {title}: {p.get('stage', '')}\n"
                f"Обработано: {p.get('processed', 0)} / {p.get('total', 0)}\n"
                f"Из store без обработки: {p.get('reused', 0)}\n"
                f"Ошибок: {p.get('errors', 0)}")
    return "⏳ Финальная сборка (manifest, архив)…"


def fmt_done(p, returncode):
    ok = returncode == 0
    lines = ["✅ Выгрузка завершена." if ok
             else f"⚠️ sync.py завершился с кодом {returncode} — см. лог."]
    if p:
        lines.append(f"Сделок: {p.get('deals', 0)}, лидов: {p.get('leads', 0)}")
        if p.get("errors"):
            lines.append(f"Ошибок: {p['errors']} "
                         f"(объекты повторятся при следующем запуске)")
        if p.get("zip"):
            lines.append(f"Архив: {p['zip']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ мониторинг
async def monitor(msg: Message, proc: subprocess.Popen):
    """Обновляет сообщение с прогрессом, пока sync.py работает."""
    last_text = ""
    while proc.poll() is None:
        text = fmt_progress(read_progress())
        if text != last_text:
            try:
                await msg.edit_text(text)
                last_text = text
            except Exception as e:
                log.debug("edit progress: %s", e)
        await asyncio.sleep(POLL_SEC)

    try:
        os.remove(LOCK)
    except OSError:
        pass

    p = read_progress()
    try:
        await msg.edit_text(fmt_done(p, proc.returncode), reply_markup=KB)
    except Exception as e:
        log.debug("edit done: %s", e)

    zpath = latest_zip()
    if not zpath:
        await msg.answer("Архив не найден в export/ — проверьте лог прогона.")
        return
    size = os.path.getsize(zpath)
    if size <= MAX_TG_FILE:
        await msg.answer_document(
            FSInputFile(zpath, filename=os.path.basename(zpath)),
            caption=f"Снимок ({size / 1024 / 1024:.1f} МБ)")
    else:
        await msg.answer(
            f"Архив {size / 1024 / 1024:.1f} МБ — больше лимита Telegram.\n"
            f"Заберите с сервера:\n{zpath}")


# ------------------------------------------------------------------ handlers
bot = Bot(TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(m: Message):
    if not allowed(m.chat.id):
        log.warning("Чужой чат %s", m.chat.id)
        return
    pid = running_pid()
    text = (f"⚙️ Выгрузка сейчас идёт (PID {pid})." if pid
            else "Сервис выгрузки из Bitrix24")
    await m.answer(text, reply_markup=KB)


@dp.callback_query(F.data == "run")
async def on_run(q: CallbackQuery):
    await q.answer()
    if not allowed(q.message.chat.id):
        log.warning("Чужой чат %s", q.message.chat.id)
        return

    pid = running_pid()
    if pid:
        await q.message.edit_text(
            f"⚠️ Выгрузка уже идёт (PID {pid}) — дождись завершения.",
            reply_markup=KB)
        return

    # сброс прогресса прошлого прогона
    try:
        os.remove(PROGRESS)
    except OSError:
        pass

    logf = open(RUN_LOG, "a", encoding="utf-8")
    logf.write(f"\n===== запуск из Telegram-бота "
               f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    logf.flush()
    proc = subprocess.Popen(
        [sys.executable, "sync.py"], cwd=BASE,
        stdout=logf, stderr=subprocess.STDOUT,
        env=os.environ.copy())
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    log.info("sync.py запущен из бота, PID %d", proc.pid)

    msg = await q.message.edit_text("🚀 Выгрузка запущена…")
    asyncio.create_task(monitor(msg, proc))


# ------------------------------------------------------------------ main
def main():
    if not TOKEN:
        raise SystemExit("Не задан TG_BOT_TOKEN (env или .env)")
    if not ALLOWED:
        raise SystemExit("Не задан TG_CHAT_ID (env или .env) — "
                         "без него кнопкой мог бы воспользоваться любой")
    log.info("Бот запущен (разрешённые чаты: %s)", sorted(ALLOWED))
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()