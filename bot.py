"""Telegram-бот. aiogram 3.x

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=123:abc python bot.py

Как играть:
    /solo             партия против ИИ-бота (без рейтинга; ТОЛЬКО для админов)
    /queue            рейтинговая партия со случайным соперником
    /duel             приглашение другу (ссылка) или стол в группе; без рейтинга
    /surrender        выйти из записи (засчитывается поражение)
    /rules /items     инструктаж и справка по предметам
    /me /top          рейтинг
    /archive          архив: примечания, помехи и концы плёнки, которые ты уже видел
    /admin            пульт оператора (только для админов, только в личке)
    /backup /restore  копия записи и её возврат (только для админов, только в личке)
На /start в личке сначала гифка-заставка (файл start.gif рядом с bot.py, путь можно задать START_GIF),
под сообщением кнопки; админам добавляется кнопка «Админ панель».

Переменные окружения: BOT_TOKEN (обязательна), TURN_SECONDS (30), DB_PATH (ratings.db),
SHOW_ENEMY_ITEMS (0; поставь 1, чтобы видеть предметы соперника, а не только их число),
PORT (если задана, например на Render, бот открывает порт и отвечает "ok" на любой запрос:
сюда можно направить пингер вроде UptimeRobot, чтобы бесплатный сервис не засыпал).

Бот-соперник: /solo или просто /ai без вопроса, ТОЛЬКО для админов (ADMIN_ID).
Остальным бот отвечает, что такой записи в каталоге нет. Ходит сам по логике из
engine.py (bot_move): считает вероятности по лотку, запоминает, как ты врёшь и когда отказываешься,
пользуется предметами. Ключи нейросетей ему не нужны. Рейтинг за такие партии не меняется.

Админ-функции: админ панель (кнопка на /start или /admin: партия с ИИ, вопрос ИИ, состояние)
и /ai <вопрос> (только в личке и только для ADMIN_ID). Ответ даёт ИИ: провайдеры
идут по кругу OpenRouter -> Gemini -> Groq -> Cohere -> снова OpenRouter и так до первого ответа,
у каждого AI_TIMEOUT секунд (100). Кнопка «Стоп» прерывает круг.
Секреты для неё: ADMIN_ID (можно несколько через запятую), OPENROUTER_API_KEY, GEMINI_API_KEY,
GROQ_API_KEY + GROQ_MODEL, COHERE_API_KEY. Без ключа провайдер просто пропускается.
Необязательные: OPENROUTER_MODEL (openrouter/auto), GEMINI_MODEL (gemini-flash-latest),
COHERE_MODEL (если пусто, берётся первая chat-модель из списка Cohere), AI_TIMEOUT (100).

Копия записи (только админам): раз в сутки (BACKUP_AT, время UTC, по умолчанию 05:00) бот
присылает файл с реестром, архивами игроков и идущими партиями, но только если с прошлой копии
что-то изменилось; если ничего не произошло, молчит. Файл можно получить и вручную: кнопка
«Бэкап» в админ панели или /backup. Вернуть всё после перезапуска (даже если диск стёрт): пришли
этот файл боту и напиши /restore (подписью к файлу или ответом на него). Без файла /restore
берёт последний автосейв с диска (STATE_PATH, по умолчанию state.json, пишется каждые
AUTOSAVE_SECONDS=15 с). Партии старше RESTORE_MAX_AGE_H=6 часов не возвращаются, реестр и
архив возвращаются всегда: у кого сыграно больше партий, того строка и остаётся.
Если бот перезапустился при идущих партиях, он сам напишет админам. Больше ничего без повода.

Партии живут в памяти; чтобы пережить перезапуск, нужен /restore (см. выше).
Стол каждого игрока это отдельное сообщение в личке бота, которое редактируется на месте.
Игровая логика лежит в engine.py и ничего не знает про Telegram.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

import aiohttp                      # ставится вместе с aiogram, отдельно в requirements не нужен
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from engine import (
    ARCHIVE,
    ARCHIVE_GROUPS,
    BITTER,
    CLEAN,
    ITEM_HELP,
    ITEM_NAMES,
    ITEM_WHO,
    NOTE_MARK,
    PILL_MARK,
    PILL_NAME,
    RESTORE_NOTES,
    Game,
    Result,
    bot_move,
    cut_line,
    stamp,
)

TURN_SECONDS = float(os.getenv("TURN_SECONDS", "30"))
DB_PATH = os.getenv("DB_PATH", "ratings.db")
START_RATING = 1000
ELO_K = 24
SHOW_ENEMY_ITEMS = os.getenv("SHOW_ENEMY_ITEMS", "0") == "1"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()      # из секретов часто приезжает лишний пробел/перенос


ADMIN_IDS = {int(x) for x in _env("ADMIN_ID").replace(",", " ").split() if x.isdigit()}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


AI_TIMEOUT = float(_env("AI_TIMEOUT", "100"))     # секунд на ответ каждому провайдеру
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "openrouter/auto")   # авто-выбор модели на стороне OpenRouter
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-flash-latest")       # алиас: всегда указывает на свежий Flash
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL")                                  # обязательна для Groq, задаётся в секретах
COHERE_API_KEY = _env("COHERE_API_KEY")
COHERE_MODEL = _env("COHERE_MODEL")                              # пусто = авто (первая chat-модель из списка)

BOT_UID = -1            # фиктивный id ИИ-соперника: настоящих пользователей с таким id не бывает
BOT_NAME = "ИИ"

log = logging.getLogger("game")
router = Router()
BOT: Bot | None = None
BOT_USERNAME = ""


# ======================================================================
# Рейтинг (SQLite)
# ======================================================================

def _run(sql: str, args: tuple = (), fetch: str | None = None):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS players ("
            "uid INTEGER PRIMARY KEY, name TEXT, rating INTEGER, wins INTEGER, losses INTEGER)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS archive ("
            "uid INTEGER, entry TEXT, ts INTEGER, PRIMARY KEY(uid, entry))"
        )
        cur = con.execute(sql, args)
        out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
        con.commit()
        return out
    finally:
        con.close()


def get_rating(uid: int) -> int:
    row = _run("SELECT rating FROM players WHERE uid=?", (uid,), "one")
    return row[0] if row else START_RATING


def touch_player(uid: int, name: str) -> None:
    _run(
        "INSERT INTO players(uid,name,rating,wins,losses) VALUES(?,?,?,0,0) "
        "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
        (uid, name, START_RATING),
    )


def apply_result(winner: int, loser: int) -> tuple[int, int]:
    """Возвращает изменения рейтинга (победитель, проигравший)."""
    rw, rl = get_rating(winner), get_rating(loser)
    exp_w = 1 / (1 + 10 ** ((rl - rw) / 400))
    delta = round(ELO_K * (1 - exp_w))
    _run("UPDATE players SET rating=rating+?, wins=wins+1 WHERE uid=?", (delta, winner))
    _run("UPDATE players SET rating=rating-?, losses=losses+1 WHERE uid=?", (delta, loser))
    return delta, -delta


# ======================================================================
# Сессии
# ======================================================================

@dataclass
class Session:
    gid: str
    game: Game
    uids: tuple[int, int]
    names: tuple[str, str]
    ranked: bool
    group_ref: tuple[int, int] | None = None
    msg_ids: list = field(default_factory=lambda: [None, None])
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None
    bot: int | None = None                    # номер места, которое играет ИИ (None: два человека)
    bot_task: asyncio.Task | None = None
    new_finds: list = field(default_factory=lambda: [0, 0])    # сколько записей архива нашёл каждый


sessions: dict[str, Session] = {}
user_game: dict[int, str] = {}
queue: list[tuple[int, str]] = []
invites: dict[str, tuple[int, str, tuple[int, int] | None]] = {}
ai_wait: set[int] = set()        # админы, от которых админ-панель ждёт вопрос для ИИ
finished: dict[str, tuple] = {}           # gid -> (uids, names, ranked): для кнопки «Реванш»
rematch_wants: dict[str, set[int]] = {}   # gid -> кто уже нажал «Реванш»


def short_name(user) -> str:
    return (user.full_name or str(user.id))[:20]


def deco(text: str, key: str | None = None, cut: float = 0.0) -> str:
    """Каталожное оформление экрана вне партии. Сам текст не меняется:
    key даёт штамп сверху (инвентарный номер), cut это шанс получить снизу
    строку, оборванную на полуслове, как будто на этом месте кончилась плёнка."""
    out = text
    if key:
        out = f"<code>{escape(stamp(key))}</code>\n{out}"
    if cut and random.random() < cut:
        out += f"\n\n<i>{escape(cut_line())}</i>"
    return out


def block(*lines: str) -> str:
    """Выделенный блок. Telegram рисует <pre> отдельной плашкой с кнопкой «копировать»."""
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def cb_chat_type(cb: CallbackQuery) -> str | None:
    return cb.message.chat.type if cb.message else None


def cb_say(cb: CallbackQuery):
    """Аналог message.answer для нажатия кнопки: пишет в тот же чат, где нажали."""
    chat_id = cb.message.chat.id if cb.message else cb.from_user.id

    async def say(text: str, **kw):
        return await BOT.send_message(chat_id, text, **kw)

    return say


# ======================================================================
# Отрисовка
# ======================================================================

def bar(n: int, total: int) -> str:
    return "▮" * n + "▯" * (total - n)


def tray_view(g: Game) -> str:
    parts = []
    for kind in (CLEAN, BITTER):
        if g.tray[kind]:
            parts.append(PILL_MARK[kind] * g.tray[kind])
    return " ".join(parts) + f"  ({g.tray_total()})"


def inv(g: Game, p: int) -> str:
    out = []
    for kind in ITEM_NAMES:
        c = g.count(p, kind)
        if c:
            out.append(ITEM_NAMES[kind] + (f" ×{c}" if c > 1 else ""))
    return ", ".join(out) or "нет"


def inv_hidden(g: Game, p: int) -> str:
    n = len(g.items[p])
    return f"{n} шт., что именно не видно" if n else "нет"


WAIT_DEAL = [
    "Выдаёт {on}. Слышно, как перебирают таблетки.",
    "Выдаёт {on}. Не торопится.",
    "Выдаёт {on}. На плёнке это длится дольше, чем было.",
]
WAIT_DECIDE = [
    "Ждёшь решения: {on}.",
    "Ждёшь решения: {on}. Взгляд на таблетке, не на тебе.",
    "Ждёшь решения: {on}. Пауза фиксируется.",
]


def render(sess: Session, i: int) -> tuple[str, InlineKeyboardMarkup | None]:
    g = sess.game
    o = 1 - i
    on = escape(sess.names[o])

    def who(p: int) -> str:
        return "ты" if p == i else sess.names[p]

    def say(line: str) -> str:
        # строки с NOTE_MARK (примечания оператора, помехи плёнки) идут курсивом
        if line.startswith(NOTE_MARK):
            return f"<i>{escape(line[len(NOTE_MARK):].format(who(0), who(1)))}</i>"
        return escape(line.format(who(0), who(1)))

    if g.over:
        lines = [
            "<b>ЗАПИСЬ ОКОНЧЕНА</b>",
            f"Курсы: ты {g.wins[i]} · {on} {g.wins[o]}",
            "",
        ]
        lines.extend(say(x) for x in g.log[-6:])
        return "\n".join(lines), None

    # панель в духе экранной индикации камеры
    panel = [
        f"● ПЛЁНКА {g.tape_no} · {g.tape_date} · {g.tape_clock()}",
        f"КУРС {g.round_no}/3 · ВЫДАЧА {g.exchange_no + 1} · СЧЁТ {g.wins[i]}:{g.wins[o]}",
        f"ОСТАТОК  {g.tape_bar()}",
        "РАССУДОК",
        f"  {'ты':<8}{bar(g.charges[i], g.max_charges)}",
        f"  {sess.names[o][:8]:<8}{bar(g.charges[o], g.max_charges)}",
        f"ЛОТОК    {tray_view(g)}",
    ]
    lines = ["<pre>" + escape("\n".join(panel)) + "</pre>"]

    # помеха плёнки: зависит от состояния, перерисовка её не меняет
    glitch = g.anomaly(i)
    if glitch:
        lines.append(say(NOTE_MARK + glitch))

    if g.round_no >= 2:
        theirs = inv(g, o) if SHOW_ENEMY_ITEMS else inv_hidden(g, o)
        lines.append(f"Предметы: ты — {inv(g, i)}; {on} — {theirs}")

    lines.append("")
    lines.extend(say(x) for x in g.log[-6:])
    lines.append("")

    if g.phase == "deal":
        pending = []
        if g.stake == 2:
            pending.append("Расписка: ставка ×2")
        if g.forced:
            pending.append("Пункт 7: отказ невозможен")
        if pending:
            lines.append(block("Стоит: " + "; ".join(pending) + "."))
        if g.turn == i:
            lines.append(f"<b>Твоя выдача.</b> Выбери таблетку и что о ней сказать. {TURN_SECONDS:g} с.")
        else:
            lines.append(WAIT_DEAL[(g.exchange_no + g.round_no) % len(WAIT_DEAL)].format(on=on))
    else:
        off = g.offer
        flags = []
        if off.stake == 2:
            flags.append("расписка ×2")
        if off.forced:
            flags.append("отказ невозможен")
        if off.stake == 0:
            flags.append("последствия вырезаны")
        flag_txt = (" · " + ", ".join(flags)) if flags else ""
        told = PILL_NAME[off.label].upper()
        # ×2 бьёт насмерть, если у кого-то рассудка не больше двух
        deadly = off.stake == 2 and any(c <= 2 for c in g.charges)
        if g.turn == i:
            if off.auto:
                lines.append(f"{on} протягивает таблетку. Время вышло: выдача автоматическая, слово честное.")
            else:
                think = f"Выбирал {off.think} с." if off.think >= 1 else "Выбрано сразу."
                lines.append(f"{on} протягивает таблетку. {think}")
            lines.append(f"[ ▒ ] Названо: «{told}»{flag_txt}")
            if deadly:
                lines.append("Расписка: курс может закончиться этой выдачей.")
            if g.lied is not None:
                # итог Диктофона отдельной плашкой: его видно сразу, и его можно скопировать
                lines.append(block(
                    "▒ ДИКТОФОН ▒",
                    f"Названо: «{told}»",
                    "Голос дрогнул." if g.lied else "Голос ровный.",
                    "ВЕРДИКТ: " + ("ЛОЖЬ" if g.lied else "ПРАВДА"),
                ))
            lines.append(f"<b>Твоё решение.</b> {TURN_SECONDS:g} с. Молчание считается согласием.")
            if off.stake == 0:
                lines.append("<i>Последствия вырезаны: что бы ты ни выбрал, ничего не изменится. Таблетку всё равно вскроют.</i>")
            elif g.exchange_no < 2:
                lines.append("<i>Принять: горькая бьёт тебя. Отказаться: горькая бьёт соперника, чистая бьёт тебя.</i>")
        else:
            lines.append(
                f"Ты протянул: {PILL_MARK[off.pill]} {PILL_NAME[off.pill]}. "
                f"Названо: «{told}»{flag_txt}"
            )
            if off.auto:
                lines.append("Время вышло: выдача автоматическая, слово честное.")
            if deadly:
                lines.append("Расписка: курс может закончиться этой выдачей.")
            lines.append(WAIT_DECIDE[(g.exchange_no + g.round_no) % len(WAIT_DECIDE)].format(on=on))

    return "\n".join(lines), keyboard(sess, i)


def keyboard(sess: Session, i: int) -> InlineKeyboardMarkup | None:
    g = sess.game
    if g.over or g.turn != i:
        return None
    gid = sess.gid
    rows: list[list[InlineKeyboardButton]] = []

    def btn(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"g:{gid}:{data}")

    if g.phase == "deal":
        acc = {CLEAN: "чистую", BITTER: "горькую"}
        for pill in g.pills():
            for label in (CLEAN, BITTER):
                rows.append([
                    btn(f"{PILL_MARK[pill]} {acc[pill].capitalize()} · назвать «{PILL_NAME[label]}»",
                        f"o:{pill}{label}")
                ])
    else:
        if g.offer.forced:
            rows.append([btn("Принять", "t")])
        else:
            rows.append([btn("Принять", "t"), btn("Отказаться", "r")])

    row: list[InlineKeyboardButton] = []
    for kind in ITEM_NAMES:
        if g.can_use(i, kind)[0]:
            c = g.count(i, kind)
            row.append(btn(ITEM_NAMES[kind] + (f" ×{c}" if c > 1 else ""), f"u:{kind}"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def safe_edit(chat_id: int, msg_id: int | None, text: str, kb) -> None:
    if msg_id is None:
        return
    for attempt in range(2):
        try:
            await BOT.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            log.warning("edit failed: %s", e)
            return
        except TelegramForbiddenError:
            return


async def refresh(sess: Session) -> None:
    jobs = []
    for i in (0, 1):
        if i == sess.bot:
            continue                          # у ИИ нет чата, рисовать нечего
        text, kb = render(sess, i)
        note_finds(sess, i)
        jobs.append(safe_edit(sess.uids[i], sess.msg_ids[i], text, kb))
    await asyncio.gather(*jobs, return_exceptions=True)


# ======================================================================
# Таймер хода
# ======================================================================

def schedule_timer(sess: Session) -> None:
    current = asyncio.current_task()
    if sess.timer and sess.timer is not current:
        sess.timer.cancel()
    if sess.game.over:
        return
    sess.timer = asyncio.create_task(turn_timeout(sess, sess.game.decision_no))
    # каждый раз, когда очередь доходит до ИИ, запускаем его шаг (таймер выше остаётся страховкой)
    if sess.bot is not None and sess.game.turn == sess.bot:
        if sess.bot_task and sess.bot_task is not current:
            sess.bot_task.cancel()
        sess.bot_task = asyncio.create_task(bot_play(sess, sess.game.decision_no))


async def bot_play(sess: Session, token: int) -> None:
    """Один шаг ИИ: «подумал», сделал ход. После хода очередь снова может быть за ним
    (предмет, потом выдача), тогда schedule_timer запустит следующий шаг."""
    g = sess.game
    try:
        await asyncio.sleep(min(random.uniform(1.2, 3.5), TURN_SECONDS * 0.25))
    except asyncio.CancelledError:
        return
    async with sess.lock:
        if g.over or g.turn != sess.bot or g.decision_no != token:
            return
        try:
            res = bot_move(g, sess.bot)
        except Exception:
            log.exception("ИИ упал на ходу")
            return                            # партию доиграет таймер: auto_move
        if not res.ok:
            log.warning("ИИ не смог сходить: %s", res.err)
            return
        await refresh(sess)
        if g.over:
            await finish(sess)
        else:
            schedule_timer(sess)


async def turn_timeout(sess: Session, token: int) -> None:
    try:
        await asyncio.sleep(TURN_SECONDS)
    except asyncio.CancelledError:
        return
    async with sess.lock:
        g = sess.game
        if g.over or g.decision_no != token:
            return
        g.auto_move(g.turn)
        await refresh(sess)
        if g.over:
            await finish(sess)
        else:
            schedule_timer(sess)


# ======================================================================
# Старт и конец партии
# ======================================================================

async def start_session(
    a: tuple[int, str], b: tuple[int, str], ranked: bool, group_ref=None, bot: int | None = None
) -> Session:
    gid = secrets.token_hex(3)
    game = Game([a[1], b[1]])               # первую выдачу определяет жребий
    sess = Session(gid, game, (a[0], b[0]), (a[1], b[1]), ranked, group_ref, bot=bot)
    sent: list[Message] = []
    try:
        for i in (0, 1):
            if i == bot:
                continue                    # ИИ сообщений не получает
            text, kb = render(sess, i)
            m = await BOT.send_message(sess.uids[i], text, reply_markup=kb)
            sess.msg_ids[i] = m.message_id
            sent.append(m)
    except (TelegramForbiddenError, TelegramBadRequest):
        for m in sent:
            try:
                await BOT.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
        raise
    sessions[gid] = sess
    for i in (0, 1):
        if i != bot:
            user_game[sess.uids[i]] = gid
    schedule_timer(sess)                    # если первая выдача за ИИ, заодно запустит его
    return sess


async def start_solo(uid: int, name: str) -> Session:
    """Партия человека против ИИ. Человек на месте 0, ИИ на месте 1, без рейтинга."""
    queue[:] = [q for q in queue if q[0] != uid]      # из очереди выходим, раз играем с ИИ
    return await start_session((uid, name), (BOT_UID, BOT_NAME), False, bot=1)


async def finish(sess: Session) -> None:
    g = sess.game
    if sess.timer and sess.timer is not asyncio.current_task():
        sess.timer.cancel()
    if sess.bot_task and sess.bot_task is not asyncio.current_task():
        sess.bot_task.cancel()
    extra = ["", ""]
    if sess.ranked and g.winner is not None:
        w, l = g.winner, 1 - g.winner
        dw, dl = apply_result(sess.uids[w], sess.uids[l])
        extra[w] = f"\nРейтинг: {get_rating(sess.uids[w])} ({dw:+d})"
        extra[l] = f"\nРейтинг: {get_rating(sess.uids[l])} ({dl:+d})"
    if sess.bot is None:
        remember_finished(sess)
    for i in (0, 1):
        if i == sess.bot:
            continue
        note_finds(sess, i)
        won = g.winner == i
        text, _ = render(sess, i)
        text += f"\n\n<b>{'Победа' if won else 'Поражение'}.</b>{extra[i]}"
        text += "\n<i>" + ("Дверь не заперта. Можно идти." if won else "Запись продолжится без тебя.") + "</i>"
        text += f"\n<i>{g.signoff()}</i>"
        if sess.new_finds[i]:
            text += f"\nАрхив: найдено записей {sess.new_finds[i]}. /archive"
        text += f"\nПлёнка {g.tape_no} будет перезаписана."
        text += "\n" + block(*g.tape_end().split("\n"))
        again = ("Ещё партию с ИИ", "q:solo") if sess.bot is not None else ("Ещё партию", "q:again")
        row = [InlineKeyboardButton(text=again[0], callback_data=again[1])]
        if sess.bot is None:
            row.append(InlineKeyboardButton(text="Реванш", callback_data=f"rm:{sess.gid}"))
        await safe_edit(sess.uids[i], sess.msg_ids[i], text, InlineKeyboardMarkup(inline_keyboard=[row]))
    if sess.group_ref:
        w = escape(sess.names[g.winner])
        await safe_edit(
            sess.group_ref[0],
            sess.group_ref[1],
            f"Стол закрыт. Запись за {w}, курсы {g.wins[g.winner]}:{g.wins[1 - g.winner]}.\n<i>Плёнка закончилась.</i>",
            None,
        )
    for uid in sess.uids:
        user_game.pop(uid, None)
    sessions.pop(sess.gid, None)


# ======================================================================
# Команды
# ======================================================================

RULES = (
    "<b>ИНСТРУКТАЖ</b>\n"
    "Матч до двух выигранных курсов. У каждого рассудок: в курсах 1 и 2 по 2 единицы, "
    "в курсе 3 — три. Рассудок на нуле: курс проигран.\n\n"
    "<b>Лоток.</b> В нём чистые ○ и горькие ● таблетки. Состав известен обоим "
    "и обновляется после каждой выдачи. Пока решение не принято, выданная таблетка числится в лотке. "
    "Когда остался один вид, выбора нет: лоток убирают и заряжают новый.\n\n"
    "<b>Выдача.</b> Выдающий берёт таблетку, называет её («чистая» или «горькая») "
    "и протягивает. Назвать можно что угодно. Принимающий решает:\n"
    "• Принять. Горькая: −1 рассудок ему. Чистая: ничего.\n"
    "• Отказаться. Таблетку вскрывают. Горькая: её принимает выдающий, −1 ему. "
    "Чистая: отказ необоснован, −1 отказавшемуся.\n"
    "Потом роли меняются.\n\n"
    "Со 2 курса каждому выдаются предметы, все разные (/items). У соперника видно только их число. "
    "К концу курса они сгорают. Каждый предмет даёт новое время на ход.\n\n"
    f"На решение {TURN_SECONDS:g} с. Молчание считается согласием. "
    "Не успел выдающий: таблетка наугад, слово честное. "
    "Два раза подряд: курс проигран. "
    "Проигравший курс выдаёт первым. Первую выдачу определяет жребий.\n\n"
    "<i>Поставщик таблеток: ACME. Претензии не принимаются.</i>\n"
    "<i>Согласие действует до конца записи. Запись заканчивается, когда ▒▒▒▒▒▒▒▒.</i>"
)


def items_text() -> str:
    lines = ["<b>ПРЕДМЕТЫ</b>", "Выдаются со 2 курса: два, затем три. Все разные. У соперника видно только число."]
    for n, (kind, name) in enumerate(ITEM_NAMES.items(), 1):
        lines.append(
            f"\n<b>№{n:02d} · {name}</b>\nКому: {ITEM_WHO[kind]}.\n{escape(ITEM_HELP[kind])}"
        )
    lines.append("\nНомера с 06 в каталоге отсутствуют.")
    return "\n".join(lines)


@router.message(CommandStart(deep_link=True))
async def cmd_start_link(m: Message, command: CommandObject):
    touch_player(m.from_user.id, short_name(m.from_user))
    arg = command.args or ""
    if not arg.startswith("j_"):
        return await cmd_start(m)
    code = arg[2:]
    invite = invites.get(code)
    if not invite:
        return await m.answer("Приглашение не найдено или уже использовано.")
    host_id, host_name, group_ref = invite
    if host_id == m.from_user.id:
        return await m.answer("Это твоё приглашение. Отправь ссылку сопернику.")
    if m.from_user.id in user_game or host_id in user_game:
        return await m.answer("Кто-то из вас уже за столом.")
    invites.pop(code, None)
    try:
        await start_session(
            (host_id, host_name), (m.from_user.id, short_name(m.from_user)), False, group_ref
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await m.answer("Не удалось начать: сопернику нужно открыть бота и нажать /start.")


# Гифка-заставка на /start (в личке, каждый раз). Файл лежит рядом с bot.py; нет файла: бот просто
# не шлёт гифку, на /start это не влияет. После первой отправки Telegram отдаёт file_id.
START_GIF = os.getenv("START_GIF", os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.gif"))
_start_gif_id: str | None = None


async def send_start_gif(chat_id: int) -> None:
    global _start_gif_id
    try:
        if _start_gif_id:
            await BOT.send_animation(chat_id, _start_gif_id)
        elif os.path.exists(START_GIF):
            msg = await BOT.send_animation(chat_id, FSInputFile(START_GIF))
            media = msg.animation or msg.document
            if media:
                _start_gif_id = media.file_id
    except Exception as e:                       # заставка украшение: сбой не должен ломать /start
        log.warning("start gif: %s", e)


START_TEXT = (
    "Испытание на двоих.\n"
    "Один выдаёт таблетку и называет её. Второй решает: принять или отказаться.\n"
    "Согласие уже подписано.\n\n"
    "/queue — рейтинговая запись\n/duel — позвать второго\n"
    "/rules — инструктаж\n/items — предметы"
)


def start_text() -> str:
    return deco(START_TEXT, "start", cut=1.0)


def start_kb(uid: int, private: bool = True) -> InlineKeyboardMarkup:
    def b(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    rows = [
        [b("Рейтинговая запись", "m:queue")],
        [b("Позвать второго", "m:duel")],
        [b("Инструктаж", "m:rules"), b("Предметы", "m:items")],
        [b("Карточка", "m:me"), b("Реестр", "m:top")],
        [b("Архив", "m:archive")],
    ]
    if private and is_admin(uid):               # кнопку видят только админы и только в личке
        rows.append([b("Админ панель", "adm:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(m: Message):
    touch_player(m.from_user.id, short_name(m.from_user))
    if m.chat.type == ChatType.PRIVATE:
        await send_start_gif(m.chat.id)
    await m.answer(
        start_text(),
        reply_markup=start_kb(m.from_user.id, m.chat.type == ChatType.PRIVATE),
    )


@router.message(Command("rules"))
async def cmd_rules(m: Message):
    await m.answer(deco(RULES, "rules"))


@router.message(Command("items"))
async def cmd_items(m: Message):
    await m.answer(deco(items_text(), "items"))


async def do_me(user: User, say) -> None:
    touch_player(user.id, short_name(user))
    row = _run("SELECT rating,wins,losses FROM players WHERE uid=?", (user.id,), "one")
    await say(deco(
        f"<b>Карточка</b>\nРейтинг {row[0]}. Побед {row[1]}, поражений {row[2]}.",
        f"me:{user.id}", cut=0.5,
    ))


@router.message(Command("me"))
async def cmd_me(m: Message):
    await do_me(m.from_user, m.answer)


async def do_top(say) -> None:
    rows = _run(
        "SELECT name,rating,wins,losses FROM players WHERE wins+losses>0 "
        "ORDER BY rating DESC LIMIT 10",
        (),
        "all",
    )
    if not rows:
        return await say(deco("Реестр пуст. Пока никто не сыграл.", cut=1.0))
    text = "\n".join(
        f"{n}. {escape(r[0])} — {r[1]} ({r[2]}/{r[3]})" for n, r in enumerate(rows, 1)
    )
    await say(deco("<b>Реестр</b>\n" + text, "top", cut=0.5))


@router.message(Command("top"))
async def cmd_top(m: Message):
    await do_top(m.answer)


WAIT_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Выйти из очереди", callback_data="q:cancel")]]
)


async def do_queue(user: User, chat_type: str, say) -> None:
    if chat_type != ChatType.PRIVATE:
        return await say("Рейтинговая очередь работает в личке бота.")
    uid = user.id
    me = (uid, short_name(user))
    touch_player(*me)
    if uid in user_game:
        return await say("Ты уже за столом.")
    if any(q[0] == uid for q in queue):
        return await say("Ты уже в очереди. /cancel — выйти.", reply_markup=WAIT_KB)
    while queue:
        other = queue.pop(0)
        if other[0] == uid or other[0] in user_game:
            continue
        try:
            await start_session(other, me, True)
            return
        except (TelegramForbiddenError, TelegramBadRequest):
            continue            # соперник заблокировал бота, берём следующего
    queue.append(me)
    await say(
        deco("Ожидание второго участника. /cancel — выйти из очереди.", cut=0.7),
        reply_markup=WAIT_KB,
    )


@router.message(Command("queue"))
async def cmd_queue(m: Message):
    await do_queue(m.from_user, m.chat.type, m.answer)


async def do_cancel(uid: int, say) -> None:
    before = len(queue) + len(invites) + (uid in ai_wait)
    queue[:] = [q for q in queue if q[0] != uid]
    for code in [c for c, v in invites.items() if v[0] == uid]:
        invites.pop(code, None)
    ai_wait.discard(uid)
    after = len(queue) + len(invites) + (uid in ai_wait)
    await say("Отменено." if after < before else "Отменять нечего.")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    await do_cancel(m.from_user.id, m.answer)


async def do_duel(user: User, chat_type: str, say) -> None:
    uid = user.id
    me = (uid, short_name(user))
    touch_player(*me)
    if uid in user_game:
        return await say("Ты уже за столом.")
    code = secrets.token_urlsafe(5)
    if chat_type == ChatType.PRIVATE:
        invites[code] = (uid, me[1], None)
        await say(deco(
            "Отправь сопернику ссылку. Партия без рейтинга.\n"
            f"https://t.me/{BOT_USERNAME}?start=j_{code}",
            cut=0.6,
        ))
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Сесть за стол", callback_data=f"l:{code}")]]
    )
    sent = await say(f"Стол открыт: {escape(me[1])}. Нужен второй участник.", reply_markup=kb)
    invites[code] = (uid, me[1], (sent.chat.id, sent.message_id))


@router.message(Command("duel"))
async def cmd_duel(m: Message):
    await do_duel(m.from_user, m.chat.type, m.answer)


# ----- кнопки под /start -----

@router.callback_query(F.data.startswith("m:"))
async def cb_menu(cb: CallbackQuery):
    action = cb.data[2:]
    say = cb_say(cb)
    chat_type = cb_chat_type(cb) or ChatType.PRIVATE
    await cb.answer()
    if action == "queue":
        await do_queue(cb.from_user, chat_type, say)
    elif action == "duel":
        await do_duel(cb.from_user, chat_type, say)
    elif action == "rules":
        await say(deco(RULES, "rules"))
    elif action == "items":
        await say(deco(items_text(), "items"))
    elif action == "me":
        await do_me(cb.from_user, say)
    elif action == "top":
        await do_top(say)
    elif action == "archive":
        await do_archive(cb.from_user, chat_type, say)


@router.callback_query(F.data == "q:cancel")
async def cb_queue_cancel(cb: CallbackQuery):
    await cb.answer()
    if cb.message:
        try:
            await BOT.edit_message_reply_markup(
                chat_id=cb.message.chat.id, message_id=cb.message.message_id, reply_markup=None
            )
        except Exception:
            pass
    await do_cancel(cb.from_user.id, cb_say(cb))


@router.callback_query(F.data.startswith("l:"))
async def cb_lobby(cb: CallbackQuery):
    code = cb.data[2:]
    invite = invites.get(code)
    if not invite:
        return await cb.answer("Стол уже занят или закрыт.", show_alert=True)
    host_id, host_name, group_ref = invite
    uid = cb.from_user.id
    if uid == host_id:
        return await cb.answer("Ты уже за столом. Нужен второй участник.")
    if uid in user_game or host_id in user_game:
        return await cb.answer("Кто-то из вас уже за столом.", show_alert=True)
    touch_player(uid, short_name(cb.from_user))
    invites.pop(code, None)
    try:
        await start_session((host_id, host_name), (uid, short_name(cb.from_user)), False, group_ref)
    except (TelegramForbiddenError, TelegramBadRequest):
        invites[code] = invite
        return await cb.answer(
            "Не удалось начать. Обоим нужно открыть бота в личке и нажать /start.",
            show_alert=True,
        )
    await cb.answer("Запись началась в личке бота.")
    if group_ref:
        await safe_edit(group_ref[0], group_ref[1], "Запись идёт.", None)


async def do_solo(user: User, chat_type: str, say) -> None:
    """Партия против ИИ. Только для админов: остальным такой записи в каталоге нет."""
    if not is_admin(user.id):
        return await say(deco("Такой записи в каталоге нет.", cut=0.6))
    if chat_type != ChatType.PRIVATE:
        return await say("Партия с ИИ идёт в личке бота.")
    uid = user.id
    name = short_name(user)
    touch_player(uid, name)
    if uid in user_game:
        return await say("Ты уже за столом. /surrender — выйти.")
    try:
        await start_solo(uid, name)
    except (TelegramForbiddenError, TelegramBadRequest):
        await say("Не удалось начать партию. Нажми /start и попробуй ещё раз.")


@router.message(Command("solo", "bot"))
async def cmd_solo(m: Message):
    await do_solo(m.from_user, m.chat.type, m.answer)


@router.callback_query(F.data == "q:solo")
async def cb_solo_again(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        return await cb.answer("Такой записи в каталоге нет.", show_alert=True)
    if uid in user_game:
        return await cb.answer("Ты уже за столом.", show_alert=True)
    name = short_name(cb.from_user)
    touch_player(uid, name)
    try:
        await start_solo(uid, name)
    except (TelegramForbiddenError, TelegramBadRequest):
        return await cb.answer("Не удалось начать. Нажми /start.", show_alert=True)
    await cb.answer("Новая запись.")


@router.callback_query(F.data == "q:again")
async def cb_again(cb: CallbackQuery):
    await cb.answer()
    await do_queue(cb.from_user, cb_chat_type(cb) or ChatType.PRIVATE, cb_say(cb))


@router.message(Command("surrender"))
async def cmd_surrender(m: Message):
    gid = user_game.get(m.from_user.id)
    sess = sessions.get(gid) if gid else None
    if not sess:
        return await m.answer("Ты не за столом.")
    async with sess.lock:
        if sess.game.over:
            return
        sess.game.surrender(sess.uids.index(m.from_user.id))
        await refresh(sess)
        await finish(sess)


# ======================================================================
# Ходы
# ======================================================================

@router.callback_query(F.data.startswith("g:"))
async def cb_game(cb: CallbackQuery):
    parts = cb.data.split(":")
    gid, action = parts[1], parts[2]
    arg = parts[3] if len(parts) > 3 else ""
    sess = sessions.get(gid)
    if not sess:
        return await cb.answer("Запись окончена.", show_alert=True)
    if cb.from_user.id not in sess.uids:
        return await cb.answer()
    i = sess.uids.index(cb.from_user.id)
    g = sess.game

    async with sess.lock:
        if g.over:
            return await cb.answer("Запись окончена.")
        if g.turn != i:
            return await cb.answer("Сейчас не твой ход.")

        before = g.decision_no
        if action == "o" and len(arg) == 2:
            res = g.deal(i, arg[0], arg[1])          # какую таблетку и что о ней сказать
        elif action == "t":
            res = g.answer(i, True)                  # принять
        elif action == "r":
            res = g.answer(i, False)                 # отказаться
        elif action == "u":
            res = g.use(i, arg)                      # предмет
        else:
            res = Result(False, "")

        await refresh(sess)
        if res.private:
            await cb.answer(res.private)
        elif not res.ok and res.err:
            await cb.answer(res.err)
        else:
            await cb.answer()

        if g.over:
            await finish(sess)
        elif g.decision_no != before:
            schedule_timer(sess)


# ======================================================================
# ИИ для админа: /ai
# ======================================================================
# Провайдеры по кругу: OpenRouter -> Gemini -> Groq -> Cohere -> снова OpenRouter...
# У каждого AI_TIMEOUT секунд. Круги идут, пока кто-нибудь не ответит или админ не нажмёт «Стоп».

AI_STOP_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Стоп", callback_data="ai:stop")]]
)
ai_jobs: dict[int, asyncio.Task | None] = {}
_cohere_auto_model = ""


async def _http_json(http: aiohttp.ClientSession, method: str, url: str, headers: dict, body=None) -> dict:
    async with http.request(method, url, headers=headers, json=body) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")
        return await r.json(content_type=None)


async def _openai_style(http, url: str, key: str, model: str, prompt: str) -> tuple[str, str]:
    """OpenRouter и Groq говорят на одном и том же диалекте."""
    data = await _http_json(
        http, "POST", url, {"Authorization": f"Bearer {key}"},
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
    )
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
    if not text:
        raise RuntimeError(f"пустой ответ: {str(data)[:200]}")
    return text, data.get("model") or model


async def _ask_openrouter(http, prompt: str) -> tuple[str, str]:
    text, used = await _openai_style(
        http, "https://openrouter.ai/api/v1/chat/completions", OPENROUTER_API_KEY, OPENROUTER_MODEL, prompt
    )
    return text, f"openrouter · {used}"


async def _ask_gemini(http, prompt: str) -> tuple[str, str]:
    data = await _http_json(
        http, "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        {"x-goog-api-key": GEMINI_API_KEY},              # ключ в заголовке, а не в URL: не попадёт в логи
        {"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
    )
    cands = data.get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise RuntimeError(f"пустой ответ: {str(data)[:200]}")
    return text, f"gemini · {data.get('modelVersion') or GEMINI_MODEL}"


async def _ask_groq(http, prompt: str) -> tuple[str, str]:
    text, used = await _openai_style(
        http, "https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, GROQ_MODEL, prompt
    )
    return text, f"groq · {used}"


async def _ask_cohere(http, prompt: str) -> tuple[str, str]:
    global _cohere_auto_model
    headers = {"Authorization": f"Bearer {COHERE_API_KEY}"}
    model = COHERE_MODEL or _cohere_auto_model
    if not model:
        data = await _http_json(http, "GET", "https://api.cohere.com/v1/models?endpoint=chat", headers)
        names = [x["name"] for x in data.get("models") or [] if x.get("name")]
        if not names:
            raise RuntimeError("Cohere не вернул ни одной chat-модели")
        model = _cohere_auto_model = names[0]
        log.info("cohere: модель выбрана автоматически: %s", model)
    data = await _http_json(
        http, "POST", "https://api.cohere.com/v2/chat", headers,
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
    )
    parts = (data.get("message") or {}).get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    if not text:
        raise RuntimeError(f"пустой ответ: {str(data)[:200]}")
    return text, f"cohere · {model}"


def ai_providers() -> list:
    out = []
    if OPENROUTER_API_KEY:
        out.append(("OpenRouter", _ask_openrouter))
    if GEMINI_API_KEY:
        out.append(("Gemini", _ask_gemini))
    if GROQ_API_KEY and GROQ_MODEL:
        out.append(("Groq", _ask_groq))
    if COHERE_API_KEY:
        out.append(("Cohere", _ask_cohere))
    return out


async def ask_ai(prompt: str, on_try=None) -> tuple[str, str]:
    """Крутит провайдеров по кругу, пока кто-то не ответит. Возвращает (текст, чей_ответ)."""
    providers = ai_providers()
    if not providers:
        raise RuntimeError(
            "Нет ни одного рабочего ключа. Нужен хотя бы один из: OPENROUTER_API_KEY, "
            "GEMINI_API_KEY, GROQ_API_KEY + GROQ_MODEL, COHERE_API_KEY."
        )
    async with aiohttp.ClientSession() as http:
        rnd = 0
        while True:
            rnd += 1
            for name, fn in providers:
                if on_try:
                    try:
                        await on_try(name, rnd)
                    except Exception:
                        pass
                try:
                    return await asyncio.wait_for(fn(http, prompt), AI_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("ai %s: нет ответа за %g с", name, AI_TIMEOUT)
                except Exception as e:
                    log.warning("ai %s: %s", name, e)
            # весь круг мимо: чуть ждём, чтобы не долбить API без передышки
            await asyncio.sleep(min(2 * rnd, 30))


@router.message(Command("ai"))
async def cmd_ai(m: Message, command: CommandObject):
    uid = m.from_user.id
    if not is_admin(uid) or m.chat.type != ChatType.PRIVATE:
        return                                           # чужим бот не отвечает вообще
    ai_wait.discard(uid)
    prompt = (command.args or "").strip()
    if not prompt:
        return await do_solo(m.from_user, m.chat.type, m.answer)   # просто /ai = сыграть с ИИ; вопрос: /ai текст
    await run_ai(m, prompt)


async def run_ai(m: Message, prompt: str) -> None:
    """Задать вопрос ИИ от имени админа m.from_user (команда /ai или кнопка в админ панели)."""
    uid = m.from_user.id
    if uid in ai_jobs:
        return await m.answer("Прошлый запрос ещё в работе. Дождись или нажми «Стоп».")

    ai_jobs[uid] = None                                  # занимаем место до первого await
    task: asyncio.Task | None = None
    try:
        status = await m.answer("⏳ Запрос…", reply_markup=AI_STOP_KB)

        async def on_try(name: str, rnd: int) -> None:
            await safe_edit(m.chat.id, status.message_id, f"⏳ {name} · круг {rnd}", AI_STOP_KB)

        task = asyncio.create_task(ask_ai(prompt, on_try))
        ai_jobs[uid] = task
        await asyncio.wait({task})
    finally:
        ai_jobs.pop(uid, None)
        if task and not task.done():
            task.cancel()

    if task.cancelled():
        return await safe_edit(m.chat.id, status.message_id, "Остановлено.", None)
    try:
        text, label = task.result()
    except Exception as e:
        log.warning("ai: %s", e)
        return await safe_edit(m.chat.id, status.message_id, f"Ошибка: {escape(str(e))}", None)

    try:
        await BOT.delete_message(m.chat.id, status.message_id)
    except Exception:
        pass
    text = f"{text}\n\n— {label}"
    for i in range(0, len(text), 4000):                  # лимит Telegram 4096
        await m.answer(text[i:i + 4000], parse_mode=None)   # без HTML: ответ ИИ может содержать < и &


@router.callback_query(F.data == "ai:stop")
async def cb_ai_stop(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    job = ai_jobs.get(cb.from_user.id)
    if job:
        job.cancel()
    await cb.answer("Останавливаю." if job else "Нечего останавливать.")


# ======================================================================
# Архив: что игрок уже видел (примечания, помехи, концы плёнки, предметы)
# ======================================================================

ARCHIVE_SHORT = {"notes": "Примечания", "glitches": "Помехи", "ends": "Концы плёнки", "items": "Предметы"}
_found_cache: dict[int, set[str]] = {}


def _found_set(uid: int) -> set[str]:
    if uid not in _found_cache:
        rows = _run("SELECT entry FROM archive WHERE uid=?", (uid,), "all")
        _found_cache[uid] = {r[0] for r in rows}
    return _found_cache[uid]


def record_finds(uid: int, ids: list[str]) -> int:
    """Добавляет в архив игрока новые записи. Возвращает, сколько из них новые."""
    have = _found_set(uid)
    new = [e for e in dict.fromkeys(ids) if e not in have]
    now = int(time.time())
    for e in new:
        _run("INSERT OR IGNORE INTO archive(uid,entry,ts) VALUES(?,?,?)", (uid, e, now))
        have.add(e)
    return len(new)


def note_finds(sess: Session, i: int) -> None:
    """Записать в архив то, что игрок i сейчас видит. Сбой архива партию не ломает."""
    if i == sess.bot:
        return
    try:
        sess.new_finds[i] += record_finds(sess.uids[i], sess.game.finds(i))
    except Exception:
        log.exception("archive")


def found_ids(uid: int) -> list[str]:
    """Найденные записи, новые первыми."""
    rows = _run("SELECT entry FROM archive WHERE uid=? ORDER BY ts DESC, rowid DESC", (uid,), "all")
    return [r[0] for r in rows if r[0] in ARCHIVE]


def archive_main_text(uid: int) -> str:
    found = found_ids(uid)
    lines = [
        "<b>АРХИВ</b>",
        f"Найдено записей: {len(found)} из {len(ARCHIVE)}.",
        "Остальные не рекомендуются к просмотру.",
        "",
    ]
    for group, title in ARCHIVE_GROUPS.items():
        total = sum(1 for v in ARCHIVE.values() if v[0] == group)
        got = sum(1 for e in found if ARCHIVE[e][0] == group)
        lines.append(f"{escape(title)}: {got}/{total}")
    return deco("\n".join(lines), f"archive:{uid}", cut=0.5)


def archive_group_text(uid: int, group: str) -> str:
    found = [e for e in found_ids(uid) if ARCHIVE[e][0] == group]
    total = sum(1 for v in ARCHIVE.values() if v[0] == group)
    head = f"<b>{escape(ARCHIVE_GROUPS[group])}</b> · {len(found)}/{total}"
    if not found:
        return head + "\n\n▒▒▒▒▒▒▒▒▒▒▒▒\nПока ничего не найдено."
    lines: list[str] = []
    size = len(head) + 250
    for k, e in enumerate(found):
        row = "· " + escape(ARCHIVE[e][1])
        if size + len(row) > 3800:
            lines.append(f"… ещё {len(found) - k}")
            break
        lines.append(row)
        size += len(row) + 1
    rest = total - len(found)
    tail = (
        f"\n▒▒▒▒▒▒▒▒▒▒▒▒ не найдено: {rest}"
        if rest else "\nВсё найдено. Это не значит, что это всё."
    )
    return head + "\n\n" + "\n".join(lines) + tail


def archive_kb(back: bool) -> InlineKeyboardMarkup:
    def b(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    if back:
        return InlineKeyboardMarkup(inline_keyboard=[[b("Назад", "ar:main")]])
    keys = list(ARCHIVE_GROUPS)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(ARCHIVE_SHORT[k], f"ar:{k}") for k in keys[:2]],
        [b(ARCHIVE_SHORT[k], f"ar:{k}") for k in keys[2:]],
    ])


async def do_archive(user: User, chat_type: str, say) -> None:
    if chat_type != ChatType.PRIVATE:
        return await say("Архив хранится в личке бота.")
    touch_player(user.id, short_name(user))
    await say(archive_main_text(user.id), reply_markup=archive_kb(False))


@router.message(Command("archive"))
async def cmd_archive(m: Message):
    await do_archive(m.from_user, m.chat.type, m.answer)


@router.callback_query(F.data.startswith("ar:"))
async def cb_archive(cb: CallbackQuery):
    await cb.answer()
    if not cb.message or cb.message.chat.type != ChatType.PRIVATE:
        return
    view, uid = cb.data[3:], cb.from_user.id
    if view == "main":
        text, kb = archive_main_text(uid), archive_kb(False)
    elif view in ARCHIVE_GROUPS:
        text, kb = archive_group_text(uid, view), archive_kb(True)
    else:
        return
    await safe_edit(cb.message.chat.id, cb.message.message_id, text, kb)


# ======================================================================
# Реванш
# ======================================================================

def remember_finished(sess: Session) -> None:
    finished[sess.gid] = (sess.uids, sess.names, sess.ranked)
    while len(finished) > 200:
        gid = next(iter(finished))
        finished.pop(gid, None)
        rematch_wants.pop(gid, None)


@router.callback_query(F.data.startswith("rm:"))
async def cb_rematch(cb: CallbackQuery):
    gid = cb.data[3:]
    info = finished.get(gid)
    if not info:
        return await cb.answer("Плёнка уже перезаписана.", show_alert=True)
    uids, names, ranked = info
    uid = cb.from_user.id
    if uid not in uids:
        return await cb.answer()
    if uid in user_game:
        return await cb.answer("Ты уже за столом.", show_alert=True)
    me_idx = uids.index(uid)
    other = uids[1 - me_idx]
    want = rematch_wants.setdefault(gid, set())
    if uid in want:
        return await cb.answer("Просьба уже передана. Ждём второго.")
    want.add(uid)
    if len(want) == 2:
        if other in user_game:
            want.discard(uid)
            return await cb.answer("Соперник уже за другим столом.", show_alert=True)
        finished.pop(gid, None)
        rematch_wants.pop(gid, None)
        try:
            await start_session((uids[0], names[0]), (uids[1], names[1]), ranked)
        except (TelegramForbiddenError, TelegramBadRequest):
            return await cb.answer("Не удалось начать. Нажми /start.", show_alert=True)
        return await cb.answer("Новая запись.")
    await cb.answer("Просьба передана.")
    try:
        await BOT.send_message(
            other,
            deco(f"{escape(names[me_idx])} просит повторить запись.", cut=0.5),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Реванш", callback_data=f"rm:{gid}")]]
            ),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


# ======================================================================
# Копия записи: ежедневный файл админу, автосейв, /restore
# ======================================================================
# В файле: реестр игроков, архивы игроков и идущие партии. Диск на бесплатных хостингах
# стирается при перезапуске, поэтому надёжной копией считается именно файл у админа в чате.

BACKUP_KIND = "plenka-backup"
STATE_PATH = os.getenv("STATE_PATH", "state.json")
PREV_PATH = STATE_PATH + ".prev"                       # автосейв, найденный при старте
META_PATH = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backup_meta.json")
AUTOSAVE_SECONDS = float(os.getenv("AUTOSAVE_SECONDS", "15"))
RESTORE_MAX_AGE = float(os.getenv("RESTORE_MAX_AGE_H", "6")) * 3600
BACKUP_AT = _env("BACKUP_AT", "05:00")                 # время суток по UTC
PREV_STATE: dict | None = None


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def session_to_dict(sess: Session) -> dict:
    return {
        "gid": sess.gid, "game": sess.game.to_dict(),
        "uids": list(sess.uids), "names": list(sess.names), "ranked": sess.ranked,
        "group_ref": list(sess.group_ref) if sess.group_ref else None,
        "msg_ids": list(sess.msg_ids), "bot": sess.bot,
    }


def session_from_dict(d: dict) -> Session:
    return Session(
        gid=str(d["gid"]), game=Game.from_dict(d["game"]),
        uids=(int(d["uids"][0]), int(d["uids"][1])),
        names=(str(d["names"][0]), str(d["names"][1])),
        ranked=bool(d["ranked"]),
        group_ref=tuple(d["group_ref"]) if d.get("group_ref") else None,
        msg_ids=list(d.get("msg_ids") or [None, None]), bot=d.get("bot"),
    )


def build_backup() -> dict:
    return {
        "kind": BACKUP_KIND, "version": 1, "created_ts": time.time(),
        "players": [list(r) for r in _run(
            "SELECT uid,name,rating,wins,losses FROM players ORDER BY uid", (), "all")],
        "archive": [list(r) for r in _run(
            "SELECT uid,entry,ts FROM archive ORDER BY uid,entry", (), "all")],
        "sessions": [session_to_dict(s) for s in sessions.values() if not s.game.over],
    }


def fingerprint(b: dict) -> str:
    """Отпечаток «что произошло»: реестр, архивы и ход идущих партий. Время не входит."""
    core = {
        "players": b["players"], "archive": b["archive"],
        "sessions": [
            [x["gid"], x["game"]["round_no"], x["game"]["exchange_no"], x["game"]["wins"]]
            for x in b["sessions"]
        ],
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_meta() -> dict:
    meta = _read_json(META_PATH)
    return meta if isinstance(meta, dict) else {}


def save_meta(meta: dict) -> None:
    try:
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError:
        log.exception("backup meta")


def save_state_file(b: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def stash_previous_state() -> dict | None:
    """При старте: если автосейв прошлого запуска содержит незавершённые партии, прячем его
    в .prev, чтобы новый автосейв его не затёр до команды /restore."""
    b = _read_json(STATE_PATH)
    if isinstance(b, dict) and b.get("kind") == BACKUP_KIND and b.get("sessions"):
        try:
            os.replace(STATE_PATH, PREV_PATH)
        except OSError:
            log.exception("stash state")
        return b
    prev = _read_json(PREV_PATH)
    return prev if isinstance(prev, dict) and prev.get("kind") == BACKUP_KIND else None


def fresh_sessions(b: dict | None) -> int:
    """Сколько партий из копии ещё можно вернуть (копия не старше RESTORE_MAX_AGE)."""
    if not b or time.time() - float(b.get("created_ts", 0)) > RESTORE_MAX_AGE:
        return 0
    return len(b.get("sessions") or [])


def merge_tables(players: list, archive: list) -> tuple[int, int, int, int]:
    """Вливает реестр и архив из копии. Строка игрока заменяется, только если в копии у него
    больше сыгранных партий: свежую базу старой копией не откатить."""
    _run("SELECT 1", (), "one")                        # создаст таблицы, если базы ещё нет
    con = sqlite3.connect(DB_PATH)
    try:
        added = updated = kept = 0
        for uid, name, rating, wins, losses in players:
            uid, rating, wins, losses = int(uid), int(rating), int(wins), int(losses)
            cur = con.execute("SELECT wins+losses FROM players WHERE uid=?", (uid,)).fetchone()
            if cur is None:
                con.execute(
                    "INSERT INTO players(uid,name,rating,wins,losses) VALUES(?,?,?,?,?)",
                    (uid, str(name)[:40], rating, wins, losses),
                )
                added += 1
            elif wins + losses > cur[0]:
                con.execute(
                    "UPDATE players SET name=?,rating=?,wins=?,losses=? WHERE uid=?",
                    (str(name)[:40], rating, wins, losses, uid),
                )
                updated += 1
            else:
                kept += 1
        a_new = 0
        for uid, entry, ts in archive:
            c = con.execute(
                "INSERT OR IGNORE INTO archive(uid,entry,ts) VALUES(?,?,?)",
                (int(uid), str(entry), int(ts)),
            )
            a_new += c.rowcount
        con.commit()
        return added, updated, kept, a_new
    finally:
        con.close()


async def restore_refresh(sess: Session) -> None:
    """Перерисовать столы возвращённой партии на старых сообщениях. Если сообщение пропало,
    пишем новое и запоминаем его номер."""
    for i in (0, 1):
        if i == sess.bot:
            continue
        text, kb = render(sess, i)
        chat, mid = sess.uids[i], sess.msg_ids[i]
        ok = False
        if mid is not None:
            try:
                await BOT.edit_message_text(text, chat_id=chat, message_id=mid, reply_markup=kb)
                ok = True
            except TelegramForbiddenError:
                continue                                   # игрок заблокировал бота: слать некуда
            except Exception as e:
                ok = "not modified" in str(e)
        if not ok:
            try:
                m = await BOT.send_message(chat, text, reply_markup=kb)
                sess.msg_ids[i] = m.message_id
            except (TelegramForbiddenError, TelegramBadRequest):
                pass


async def restore_from(b: dict) -> str:
    added, updated, kept, a_new = merge_tables(b.get("players") or [], b.get("archive") or [])
    _found_cache.clear()
    items = b.get("sessions") or []
    fresh = time.time() - float(b.get("created_ts", 0)) <= RESTORE_MAX_AGE
    back = skipped = failed = 0
    stale = 0 if fresh else len(items)
    for d in items if fresh else []:
        try:
            gid = str(d["gid"])
            bot_seat = d.get("bot")
            humans = [int(u) for k, u in enumerate(d["uids"]) if k != bot_seat]
            if gid in sessions or any(u in user_game for u in humans):
                skipped += 1
                continue
            sess = session_from_dict(d)
            sess.game.log.append(NOTE_MARK + random.choice(RESTORE_NOTES))
            sessions[gid] = sess
            for u in humans:
                user_game[u] = gid
            await restore_refresh(sess)
            schedule_timer(sess)
            for u in humans:
                try:
                    await BOT.send_message(
                        u, "<i>Плёнка найдена. Запись продолжается с последнего сохранённого кадра.</i>"
                    )
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
            back += 1
        except Exception:
            log.exception("restore session")
            failed += 1
    rows = [
        f"Реестр: +{added}, обновлено {updated}, без изменений {kept}",
        f"Архив: +{a_new}",
        f"Партий возвращено: {back}",
    ]
    if stale:
        rows.append(f"Устарели и не возвращены: {stale}")
    if skipped:
        rows.append(f"Пропущено, стол уже занят: {skipped}")
    if failed:
        rows.append(f"Не удалось: {failed}")
    return deco("<b>ПЛЁНКА ПЕРЕМОТАНА</b>\n" + block(*rows) + "\nЗапись продолжается.", cut=0.5)


def backup_caption(b: dict, manual: bool) -> str:
    head = "КОПИЯ ПО ЗАПРОСУ ОПЕРАТОРА" if manual else "СЛУЖЕБНАЯ ПЕРЕДАЧА · КОПИЯ ЗАПИСИ"
    text = (
        f"<code>{head}</code>\n"
        + ("Копия плёнки снята сейчас." if manual else "Копия плёнки за сутки.")
        + " Хранить отдельно от самой плёнки.\n"
        f"В реестре: {len(b['players'])}. Незавершённых партий: {len(b['sessions'])}.\n"
        "Если запись оборвётся, ответьте на этот файл командой /restore."
    )
    return deco(text, cut=1.0)


async def send_backup(chat_ids: list[int], manual: bool) -> bool:
    """Отправляет файл копии. Ежедневная отправка молчит, если с прошлой копии ничего не изменилось."""
    b = build_backup()
    fp = fingerprint(b)
    if not manual and load_meta().get("fp") == fp:
        return False
    data = json.dumps(b, ensure_ascii=False).encode("utf-8")
    name = time.strftime("plenka_%Y-%m-%d_%H%M.json", time.gmtime())
    caption = backup_caption(b, manual)
    sent = 0
    for cid in chat_ids:
        try:
            await BOT.send_document(cid, BufferedInputFile(data, filename=name), caption=caption)
            sent += 1
        except Exception as e:
            log.warning("backup -> %s: %s", cid, e)
    if sent and not manual:
        save_meta({"fp": fp, "ts": time.time()})
    return sent > 0


def _backup_time() -> tuple[int, int]:
    try:
        h, m = BACKUP_AT.split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return 5, 0


async def backup_loop() -> None:
    h, m = _backup_time()
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            if ADMIN_IDS:
                await send_backup(sorted(ADMIN_IDS), manual=False)
        except Exception:
            log.exception("daily backup")


async def autosave_loop() -> None:
    last = None
    while True:
        await asyncio.sleep(AUTOSAVE_SECONDS)
        try:
            b = build_backup()
            key = json.dumps({k: v for k, v in b.items() if k != "created_ts"}, sort_keys=True)
            if key != last:
                save_state_file(b)
                last = key
        except Exception:
            log.exception("autosave")


async def notify_interrupted() -> None:
    """После перезапуска пишем админам, только если оборвались партии. Иначе молчим."""
    n = fresh_sessions(PREV_STATE)
    if not n:
        return
    text = deco(
        f"Запись прервана. Незавершённых партий: {n}. Они ещё на плёнке. "
        "/restore перемотает их на последний кадр.",
        cut=0.7,
    )
    for a in ADMIN_IDS:
        try:
            await BOT.send_message(a, text)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


@router.message(Command("backup"))
async def cmd_backup(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != ChatType.PRIVATE:
        return                                           # чужим бот не отвечает вообще
    if not await send_backup([m.from_user.id], manual=True):
        await m.answer("Копию отправить не удалось.")


@router.message(Command("restore"))
async def cmd_restore(m: Message):
    """/restore подписью к файлу копии или ответом на него. Без файла: последний автосейв с диска."""
    global PREV_STATE
    if not is_admin(m.from_user.id) or m.chat.type != ChatType.PRIVATE:
        return                                           # чужим бот не отвечает вообще
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    local = doc is None
    if doc:
        if (doc.file_size or 0) > 15_000_000:
            return await m.answer("Файл слишком тяжёлый для этой плёнки.")
        try:
            buf = await BOT.download(doc)
            b = json.loads(buf.read().decode("utf-8"))
        except Exception as e:
            log.warning("restore read: %s", e)
            return await m.answer("Файл не читается. Это не плёнка.")
    else:
        b = PREV_STATE or _read_json(PREV_PATH)
        if not b:
            return await m.answer(deco(
                "Сохранённой плёнки на месте нет. Ответьте командой /restore на файл копии.", cut=0.6))
    if not isinstance(b, dict) or b.get("kind") != BACKUP_KIND:
        return await m.answer("Это не копия записи.")
    try:
        report = await restore_from(b)
    except Exception:
        log.exception("restore")
        return await m.answer("Плёнка не перематывается. Подробности в журнале сервера.")
    if local:
        PREV_STATE = None
        try:
            os.remove(PREV_PATH)
        except OSError:
            pass
    await m.answer(report)


# ======================================================================
# Админ панель
# ======================================================================
# Кнопка на /start (видят только ADMIN_ID и только в личке) или /admin.
# Каждое действие заново проверяет, что нажал админ: спрятанной кнопки мало.

def admin_text() -> str:
    return deco(
        "<b>ПУЛЬТ ОПЕРАТОРА</b>\n"
        "Оператор найден. Это вы. Проверять не нужно.\n"
        "Эти процедуры в каталог для участников не вошли.\n\n"
        "/solo — партия против ИИ\n/ai вопрос — спросить ИИ\n"
        "/backup — копия записи файлом\n/restore — вернуть запись (подписью к файлу или ответом на него)",
        "admin", cut=1.0,
    )


def admin_kb() -> InlineKeyboardMarkup:
    def b(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    return InlineKeyboardMarkup(inline_keyboard=[
        [b("Партия с ИИ", "adm:solo")],
        [b("Спросить ИИ", "adm:ask"), b("Состояние", "adm:stat")],
        [b("Бэкап", "adm:backup"), b("Назад", "adm:back")],
    ])


def admin_status() -> str:
    live = [s for s in sessions.values() if not s.game.over]
    rows = [
        f"Столов идёт: {len(live)}",
        f"В очереди:   {len(queue)}",
        f"Приглашений: {len(invites)}",
        f"В реестре:   {_run('SELECT COUNT(*) FROM players', (), 'one')[0]}",
        "ИИ-ключи:    " + (", ".join(n for n, _ in ai_providers()) or "нет"),
    ]
    for s in live[:8]:
        g = s.game
        kind = "ИИ" if s.bot is not None else ("рейтинг" if s.ranked else "дуэль")
        rows.append(
            f"· {s.names[0][:10]} — {s.names[1][:10]} · курс {g.round_no} · "
            f"{g.wins[0]}:{g.wins[1]} · {kind} · плёнка {g.tape_no}"
        )
    if len(live) > 8:
        rows.append(f"· ещё {len(live) - 8}")
    ts = load_meta().get("ts")
    rows.append(
        "Копия:       "
        + (time.strftime("%d.%m %H:%M UTC", time.gmtime(ts)) if ts else "ещё не отправлялась")
    )
    rows.append(f"Время сервера: {time.strftime('%H:%M:%S')}")
    return "<b>СОСТОЯНИЕ ЗАПИСИ</b>\n" + block(*rows)


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != ChatType.PRIVATE:
        return                                           # чужим бот не отвечает вообще
    await m.answer(admin_text(), reply_markup=admin_kb())


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid) or cb_chat_type(cb) != ChatType.PRIVATE:
        return await cb.answer()                         # чужим кнопка не отвечает
    action = cb.data[4:]
    chat_id, msg_id = cb.message.chat.id, cb.message.message_id
    say = cb_say(cb)
    await cb.answer()
    if action == "open":
        await safe_edit(chat_id, msg_id, admin_text(), admin_kb())
    elif action == "back":
        await safe_edit(chat_id, msg_id, start_text(), start_kb(uid, True))
    elif action == "stat":
        await safe_edit(chat_id, msg_id, admin_status(), admin_kb())
    elif action == "solo":
        await do_solo(cb.from_user, ChatType.PRIVATE, say)
    elif action == "backup":
        if not await send_backup([uid], manual=True):
            await say("Копию отправить не удалось.")
    elif action == "ask":
        ai_wait.add(uid)
        await say("Следующим сообщением пришли вопрос для ИИ. /cancel — отменить.")


@router.message(F.text, ~F.text.startswith("/"))
async def msg_ai_prompt(m: Message):
    """Обычный текст от админа считается вопросом для ИИ только после кнопки «Спросить ИИ»."""
    uid = m.from_user.id
    if uid not in ai_wait or not is_admin(uid) or m.chat.type != ChatType.PRIVATE:
        return
    ai_wait.discard(uid)
    await run_ai(m, m.text.strip())


# ======================================================================
# Запуск
# ======================================================================

async def _health_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Минимальный HTTP-ответ 200 "ok" на любой запрос (GET и HEAD)."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.strip():
            return                                 # пустой запрос: просто закрываем
        method = line.split(b" ", 1)[0].upper()
        for _ in range(64):                       # дочитываем заголовки
            h = await asyncio.wait_for(reader.readline(), timeout=5)
            if h in (b"\r\n", b"\n", b""):
                break
        body = b"ok"
        head = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        writer.write(head + (b"" if method == b"HEAD" else body))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def start_health_server():
    """Открывает порт из переменной PORT. Без PORT ничего не делает."""
    port = os.getenv("PORT")
    if not port:
        return None
    server = await asyncio.start_server(_health_client, "0.0.0.0", int(port))
    log.info("health server on port %s", port)
    return server


async def main() -> None:
    global BOT, BOT_USERNAME, PREV_STATE
    token = os.environ["BOT_TOKEN"]
    logging.basicConfig(level=logging.INFO)
    PREV_STATE = stash_previous_state()      # до запуска автосейва: он не должен затереть прошлый
    log.info("AI: админы=%s, провайдеры=%s", sorted(ADMIN_IDS), [n for n, _ in ai_providers()] or "нет")
    if GROQ_API_KEY and not GROQ_MODEL:
        log.warning("GROQ_API_KEY задан, но GROQ_MODEL пуст: Groq пропускается")
    if not ADMIN_IDS:
        log.warning("ADMIN_ID не задан: режим с ИИ, /ai и админ панель никому не доступны")
    BOT = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await BOT.get_me()).username
    server = await start_health_server()
    dp = Dispatcher()
    dp.include_router(router)
    tasks = [asyncio.create_task(autosave_loop()), asyncio.create_task(backup_loop())]
    try:
        # после перезапуска старые нажатия кнопок бессмысленны: матчи хранятся в памяти
        # (вернуть их можно командой /restore, кнопки в старых сообщениях снова заработают)
        await BOT.delete_webhook(drop_pending_updates=True)
        await notify_interrupted()
        await dp.start_polling(BOT)
    finally:
        for t in tasks:
            t.cancel()
        try:
            save_state_file(build_backup())  # последний кадр перед остановкой
        except Exception:
            log.exception("final save")
        if server:
            server.close()


if __name__ == "__main__":
    asyncio.run(main())
