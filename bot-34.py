"""Telegram-бот. aiogram 3.x

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=123:abc python bot.py

Как играть:
    /solo             партия против ИИ-бота (без рейтинга; для теста и если нет друзей)
    /queue            рейтинговая партия со случайным соперником
    /duel             приглашение другу (ссылка) или стол в группе; без рейтинга
    /surrender        выйти из записи (засчитывается поражение)
    /rules /items     инструктаж и справка по предметам
    /me /top          рейтинг

Переменные окружения: BOT_TOKEN (обязательна), TURN_SECONDS (30), DB_PATH (ratings.db),
SHOW_ENEMY_ITEMS (0; поставь 1, чтобы видеть предметы соперника, а не только их число),
PORT (если задана, например на Render, бот открывает порт и отвечает "ok" на любой запрос:
сюда можно направить пингер вроде UptimeRobot, чтобы бесплатный сервис не засыпал).

Бот-соперник: /solo (для всех) или просто /ai без вопроса (для админа). Ходит сам по логике из
engine.py (bot_move): считает вероятности по лотку, запоминает, как ты врёшь и когда отказываешься,
пользуется предметами. Ключи нейросетей ему не нужны. Рейтинг за такие партии не меняется.

Админ-функция: /ai <вопрос> (только в личке и только для ADMIN_ID). Ответ даёт ИИ: провайдеры
идут по кругу OpenRouter -> Gemini -> Groq -> Cohere -> снова OpenRouter и так до первого ответа,
у каждого AI_TIMEOUT секунд (100). Кнопка «Стоп» прерывает круг.
Секреты для неё: ADMIN_ID (можно несколько через запятую), OPENROUTER_API_KEY, GEMINI_API_KEY,
GROQ_API_KEY + GROQ_MODEL, COHERE_API_KEY. Без ключа провайдер просто пропускается.
Необязательные: OPENROUTER_MODEL (openrouter/auto), GEMINI_MODEL (gemini-flash-latest),
COHERE_MODEL (если пусто, берётся первая chat-модель из списка Cohere), AI_TIMEOUT (100).

Партии живут в памяти: после перезапуска бота идущие матчи пропадают.
Стол каждого игрока это отдельное сообщение в личке бота, которое редактируется на месте.
Игровая логика лежит в engine.py и ничего не знает про Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
import sqlite3
from dataclasses import dataclass, field
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from engine import (
    BITTER,
    CLEAN,
    ITEM_HELP,
    ITEM_NAMES,
    ITEM_WHO,
    NOTE_MARK,
    PILL_MARK,
    PILL_NAME,
    Game,
    Result,
    bot_move,
)

TURN_SECONDS = float(os.getenv("TURN_SECONDS", "30"))
DB_PATH = os.getenv("DB_PATH", "ratings.db")
START_RATING = 1000
ELO_K = 24
SHOW_ENEMY_ITEMS = os.getenv("SHOW_ENEMY_ITEMS", "0") == "1"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()      # из секретов часто приезжает лишний пробел/перенос


ADMIN_IDS = {int(x) for x in _env("ADMIN_ID").replace(",", " ").split() if x.isdigit()}
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


sessions: dict[str, Session] = {}
user_game: dict[int, str] = {}
queue: list[tuple[int, str]] = []
invites: dict[str, tuple[int, str, tuple[int, int] | None]] = {}


def short_name(user) -> str:
    return (user.full_name or str(user.id))[:20]


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
            lines.append("Стоит: " + "; ".join(pending) + ".")
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
                lines.append(
                    "Диктофон: голос дрогнул. <b>Ложь.</b>" if g.lied
                    else "Диктофон: голос ровный. <b>Правда.</b>"
                )
            lines.append(f"<b>Твоё решение.</b> {TURN_SECONDS:g} с. Молчание считается согласием.")
            if g.exchange_no < 2:
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
    for i in (0, 1):
        if i == sess.bot:
            continue
        won = g.winner == i
        text, _ = render(sess, i)
        text += f"\n\n<b>{'Победа' if won else 'Поражение'}.</b>{extra[i]}"
        text += "\n<i>" + ("Дверь не заперта. Можно идти." if won else "Запись продолжится без тебя.") + "</i>"
        text += f"\n<i>{g.signoff()}</i>"
        text += f"\nПлёнка {g.tape_no} будет перезаписана."
        again = ("Ещё партию с ИИ", "q:solo") if sess.bot is not None else ("Ещё партию", "q:again")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=again[0], callback_data=again[1])]]
        )
        await safe_edit(sess.uids[i], sess.msg_ids[i], text, kb)
    if sess.group_ref:
        w = escape(sess.names[g.winner])
        await safe_edit(
            sess.group_ref[0],
            sess.group_ref[1],
            f"Стол закрыт. Запись за {w}, курсы {g.wins[g.winner]}:{g.wins[1 - g.winner]}.",
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
    lines.append("\nНомера с 05 в каталоге отсутствуют.")
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


@router.message(CommandStart())
async def cmd_start(m: Message):
    touch_player(m.from_user.id, short_name(m.from_user))
    await m.answer(
        "Испытание на двоих.\n"
        "Один выдаёт таблетку и называет её. Второй решает: принять или отказаться.\n"
        "Согласие уже подписано.\n\n"
        "/queue — рейтинговая запись\n/duel — позвать второго\n/solo — партия против ИИ\n"
        "/rules — инструктаж\n/items — предметы"
    )


@router.message(Command("rules"))
async def cmd_rules(m: Message):
    await m.answer(RULES)


@router.message(Command("items"))
async def cmd_items(m: Message):
    await m.answer(items_text())


@router.message(Command("me"))
async def cmd_me(m: Message):
    touch_player(m.from_user.id, short_name(m.from_user))
    row = _run("SELECT rating,wins,losses FROM players WHERE uid=?", (m.from_user.id,), "one")
    await m.answer(f"<b>Карточка</b>\nРейтинг {row[0]}. Побед {row[1]}, поражений {row[2]}.")


@router.message(Command("top"))
async def cmd_top(m: Message):
    rows = _run(
        "SELECT name,rating,wins,losses FROM players WHERE wins+losses>0 "
        "ORDER BY rating DESC LIMIT 10",
        (),
        "all",
    )
    if not rows:
        return await m.answer("Реестр пуст. Пока никто не сыграл.")
    text = "\n".join(
        f"{n}. {escape(r[0])} — {r[1]} ({r[2]}/{r[3]})" for n, r in enumerate(rows, 1)
    )
    await m.answer("<b>Реестр</b>\n" + text)


@router.message(Command("queue"))
async def cmd_queue(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return await m.answer("Рейтинговая очередь работает в личке бота.")
    uid = m.from_user.id
    me = (uid, short_name(m.from_user))
    touch_player(*me)
    if uid in user_game:
        return await m.answer("Ты уже за столом.")
    if any(q[0] == uid for q in queue):
        return await m.answer("Ты уже в очереди. /cancel — выйти.")
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
    await m.answer("Ожидание второго участника. /cancel — выйти из очереди.")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    before = len(queue) + len(invites)
    queue[:] = [q for q in queue if q[0] != uid]
    for code in [c for c, v in invites.items() if v[0] == uid]:
        invites.pop(code, None)
    await m.answer("Отменено." if len(queue) + len(invites) < before else "Отменять нечего.")


@router.message(Command("duel"))
async def cmd_duel(m: Message):
    uid = m.from_user.id
    me = (uid, short_name(m.from_user))
    touch_player(*me)
    if uid in user_game:
        return await m.answer("Ты уже за столом.")
    code = secrets.token_urlsafe(5)
    if m.chat.type == ChatType.PRIVATE:
        invites[code] = (uid, me[1], None)
        await m.answer(
            "Отправь сопернику ссылку. Партия без рейтинга.\n"
            f"https://t.me/{BOT_USERNAME}?start=j_{code}"
        )
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Сесть за стол", callback_data=f"l:{code}")]]
    )
    sent = await m.answer(f"Стол открыт: {escape(me[1])}. Нужен второй участник.", reply_markup=kb)
    invites[code] = (uid, me[1], (sent.chat.id, sent.message_id))


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


@router.message(Command("solo", "bot"))
async def cmd_solo(m: Message):
    if m.chat.type != ChatType.PRIVATE:
        return await m.answer("Партия с ИИ идёт в личке бота.")
    await begin_solo(m)


async def begin_solo(m: Message) -> None:
    uid = m.from_user.id
    name = short_name(m.from_user)
    touch_player(uid, name)
    if uid in user_game:
        return await m.answer("Ты уже за столом. /surrender — выйти.")
    try:
        await start_solo(uid, name)
    except (TelegramForbiddenError, TelegramBadRequest):
        await m.answer("Не удалось начать партию. Нажми /start и попробуй ещё раз.")


@router.callback_query(F.data == "q:solo")
async def cb_solo_again(cb: CallbackQuery):
    uid = cb.from_user.id
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
    await cb.answer("Жми /queue.")


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


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


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
    prompt = (command.args or "").strip()
    if not prompt:
        return await begin_solo(m)                       # просто /ai = сыграть с ИИ; вопрос: /ai текст
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
    global BOT, BOT_USERNAME
    token = os.environ["BOT_TOKEN"]
    logging.basicConfig(level=logging.INFO)
    log.info("AI: админы=%s, провайдеры=%s", sorted(ADMIN_IDS), [n for n, _ in ai_providers()] or "нет")
    if GROQ_API_KEY and not GROQ_MODEL:
        log.warning("GROQ_API_KEY задан, но GROQ_MODEL пуст: Groq пропускается")
    if not ADMIN_IDS:
        log.warning("ADMIN_ID не задан: команда /ai никому не доступна")
    BOT = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await BOT.get_me()).username
    server = await start_health_server()
    dp = Dispatcher()
    dp.include_router(router)
    try:
        # после перезапуска старые нажатия кнопок бессмысленны: матчи хранятся в памяти
        await BOT.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(BOT)
    finally:
        if server:
            server.close()


if __name__ == "__main__":
    asyncio.run(main())
