"""Telegram-бот. aiogram 3.x

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=123:abc python bot.py

Как играть:
    /queue            рейтинговая партия со случайным соперником
    /duel             приглашение другу (ссылка) или стол в группе; без рейтинга
    /surrender        сдать матч
    /rules /items     правила и справка по предметам
    /me /top          рейтинг

Партии живут в памяти: после перезапуска бота идущие матчи пропадают.
Стол каждого игрока это отдельное сообщение в личке бота, которое редактируется на месте.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from html import escape

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
from aiogram.utils.keyboard import InlineKeyboardBuilder

from engine import FAMILIES, ITEM_HELP, ITEM_NAMES, Game

TURN_SECONDS = float(os.getenv("TURN_SECONDS", "40"))
DB_PATH = os.getenv("DB_PATH", "ratings.db")
START_RATING = 1000
ELO_K = 24

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
    menu: list = field(default_factory=lambda: [None, None])
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


sessions: dict[str, Session] = {}
user_game: dict[int, str] = {}
queue: list[tuple[int, str]] = []
invites: dict[str, tuple[int, str, tuple[int, int] | None]] = {}


def short_name(user) -> str:
    return (user.full_name or str(user.id))[:20]


# ======================================================================
# Отрисовка
# ======================================================================

def faces_table() -> str:
    return "Близнецы: " + " ".join(a + b for a, b in FAMILIES)


def render(sess: Session, i: int) -> tuple[str, InlineKeyboardMarkup | None]:
    g = sess.game
    o = 1 - i
    on = escape(sess.names[o])
    lines: list[str] = []

    if g.over:
        lines.append("<b>Матч окончен</b>")
        lines.append(f"Счёт по сменам: ты {g.shifts[i]} · {on} {g.shifts[o]}")
        lines.append("")
        lines.extend(escape(x) for x in g.log[-5:])
        return "\n".join(lines), None

    lines.append(
        f"<b>Смена {g.shift}/3 · заряд {g.charge_no}</b> · смены {g.shifts[i]}:{g.shifts[o]}"
    )
    lines.append(f"Жизни: ты {g.hp[i]}/{g.max_hp} · {on} {g.hp[o]}/{g.max_hp}")

    # очередь
    parts = []
    for idx, s in enumerate(g.slots):
        if idx < g.ptr:
            parts.append(f"{idx + 1}{s.face}{'✗' if s.copy else '✓'}")
        else:
            face = "▒" if g.blind[i] else s.face
            mark = "▸" if idx == g.ptr else ""
            parts.append(f"{mark}{idx + 1}{face}")
    lines.append("Очередь: " + " ".join(parts))

    if g.blind[i]:
        lines.append("Помеха: лица скрыты на этот ход.")
    else:
        st = g.next_status()
        hint = {
            "clean": "оригинал наверняка",
            "copy": "копия наверняка",
            "unknown": "неизвестно, пара не вскрыта",
        }[st]
        lines.append(f"Следующая: {g.slots[g.ptr].face} — {hint}")

    def inv(p: int) -> str:
        if not g.items[p]:
            return "нет"
        names = []
        for kind in ITEM_NAMES:
            c = g.count(p, kind)
            if c:
                names.append(ITEM_NAMES[kind] + (f" ×{c}" if c > 1 else ""))
        return ", ".join(names)

    if g.shift >= 2:
        lines.append(f"Твои предметы: {inv(i)}")
        lines.append(f"У {on}: {inv(o)}")
    flags = []
    for p in (i, o):
        who = "ты" if p == i else on
        if g.shield[p]:
            flags.append(f"{who}: Невозможное")
        if g.skip[p]:
            flags.append(f"{who}: пропустит ход")
    if flags:
        lines.append("Статус: " + "; ".join(flags))

    notes = g.notes_for(i)
    if notes:
        lines.append("Тебе известно: " + "; ".join(notes))

    lines.append("")
    lines.extend(escape(x) for x in g.log[-4:])
    lines.append("")
    if g.turn == i:
        lines.append(f"<b>Твой ход.</b> На ход {TURN_SECONDS:g} с.")
    else:
        lines.append(f"Ход: {on}.")

    return "\n".join(lines), keyboard(sess, i)


def keyboard(sess: Session, i: int) -> InlineKeyboardMarkup | None:
    g = sess.game
    gid = sess.gid
    b = InlineKeyboardBuilder()

    def btn(text: str, data: str) -> None:
        b.button(text=text, callback_data=f"g:{gid}:{data}")

    if g.turn == i:
        menu = sess.menu[i]
        if menu == "check":
            for pid, label in g.check_options():
                btn(label, f"c:{pid}")
            b.adjust(3)
            b.row(InlineKeyboardButton(text="Назад", callback_data=f"g:{gid}:x"))
            return b.as_markup()
        if menu == "swap":
            for k in g.swap_options():
                btn(f"{g.ptr + k + 1}↔{g.ptr + k + 2}", f"s:{k}")
            b.adjust(4)
            b.row(InlineKeyboardButton(text="Назад", callback_data=f"g:{gid}:x"))
            return b.as_markup()
        if menu == "copy":
            for kind in g.copy_options(i):
                btn(ITEM_NAMES[kind], f"k:{kind}")
            b.adjust(2)
            b.row(InlineKeyboardButton(text="Назад", callback_data=f"g:{gid}:x"))
            return b.as_markup()

        b.row(
            InlineKeyboardButton(text="Принять", callback_data=f"g:{gid}:t"),
            InlineKeyboardButton(text="Отдать", callback_data=f"g:{gid}:v"),
        )
        usable = [k for k in ITEM_NAMES if g.can_use(i, k)[0]]
        if usable:
            row = []
            for kind in usable:
                c = g.count(i, kind)
                row.append(
                    InlineKeyboardButton(
                        text=ITEM_NAMES[kind] + (f" ×{c}" if c > 1 else ""),
                        callback_data=f"g:{gid}:u:{kind}",
                    )
                )
                if len(row) == 2:
                    b.row(*row)
                    row = []
            if row:
                b.row(*row)
    if g.shift >= 3:
        b.row(InlineKeyboardButton(text="Близнецы", callback_data=f"g:{gid}:f"))
    markup = b.as_markup()
    return markup if markup.inline_keyboard else None


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
        sess.menu = [None, None]
        await refresh(sess)
        if g.over:
            await finish(sess)
        else:
            schedule_timer(sess)


# ======================================================================
# Старт и конец партии
# ======================================================================

async def start_session(
    a: tuple[int, str], b: tuple[int, str], ranked: bool, group_ref=None
) -> Session:
    gid = secrets.token_hex(3)
    first = None
    if ranked:
        ra, rb = get_rating(a[0]), get_rating(b[0])
        if ra != rb:
            first = 0 if ra < rb else 1       # слабее по рейтингу ходит первым
    game = Game([a[1], b[1]], first=first)
    sess = Session(gid, game, (a[0], b[0]), (a[1], b[1]), ranked, group_ref)
    sent: list[Message] = []
    try:
        for i in (0, 1):
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
    user_game[a[0]] = gid
    user_game[b[0]] = gid
    schedule_timer(sess)
    return sess


async def finish(sess: Session) -> None:
    g = sess.game
    if sess.timer and sess.timer is not asyncio.current_task():
        sess.timer.cancel()
    extra = ["", ""]
    if sess.ranked and g.winner is not None:
        w, l = g.winner, 1 - g.winner
        dw, dl = apply_result(sess.uids[w], sess.uids[l])
        extra[w] = f"\nРейтинг: {get_rating(sess.uids[w])} ({dw:+d})"
        extra[l] = f"\nРейтинг: {get_rating(sess.uids[l])} ({dl:+d})"
    for i in (0, 1):
        won = g.winner == i
        text, _ = render(sess, i)
        text += f"\n\n<b>{'Победа' if won else 'Поражение'}.</b>{extra[i]}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Ещё партию", callback_data="q:again")]]
        )
        await safe_edit(sess.uids[i], sess.msg_ids[i], text, kb)
    if sess.group_ref:
        w = escape(sess.names[g.winner])
        await safe_edit(
            sess.group_ref[0],
            sess.group_ref[1],
            f"Стол закрыт. Матч за {w}, смены {g.shifts[g.winner]}:{g.shifts[1 - g.winner]}.",
            None,
        )
    for uid in sess.uids:
        user_game.pop(uid, None)
    sessions.pop(sess.gid, None)


# ======================================================================
# Команды
# ======================================================================

RULES = (
    "<b>Правила</b>\n"
    "Матч до двух выигранных смен. Жизней в сменах: 2, 3, 4. Смена кончается, когда у одного 0 жизней.\n\n"
    "В очереди позиции с лицами. Одиночное лицо всегда оригинал. "
    "Два одинаковых лица (в смене 3 ещё и близнецы): одна из двух позиций копия. "
    "Если одну вскрыли, вторая известна наверняка.\n\n"
    "<b>Ход.</b> Любое число предметов, затем одно из двух:\n"
    "Принять: вскрыть следующую позицию на себя. Копия: −1 жизнь, ход уходит. Оригинал: ходишь снова.\n"
    "Отдать: вскрыть на соперника. Копия: −1 ему. Оригинал: ничего. Ход уходит в любом случае.\n\n"
    "Когда очередь кончилась, выдаётся новая. Предметы со смены 2, в конце смены они сгорают. "
    f"На ход {TURN_SECONDS:g} с. Время вышло: выбор случайный. Два раза подряд: смена проиграна.\n"
    "Проигравший смену ходит первым в следующей. В рейтинге первую смену начинает игрок с меньшим рейтингом."
)


def items_text() -> str:
    lines = ["<b>Предметы</b>"]
    for k, name in ITEM_NAMES.items():
        lines.append(f"<b>{name}</b>: {escape(ITEM_HELP[k])}")
    lines.append("\nВ смене 3 Сверка может ошибаться. Ошибку можно поймать, сверив пару, одну позицию которой уже вскрыли.")
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
        "Два игрока, одна очередь позиций. Часть позиций копии.\n"
        "/queue — рейтинговая партия\n/duel — позвать друга\n/rules — правила\n/items — предметы"
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
    await m.answer(f"Рейтинг {row[0]}. Побед {row[1]}, поражений {row[2]}.")


@router.message(Command("top"))
async def cmd_top(m: Message):
    rows = _run(
        "SELECT name,rating,wins,losses FROM players WHERE wins+losses>0 "
        "ORDER BY rating DESC LIMIT 10",
        (),
        "all",
    )
    if not rows:
        return await m.answer("Пока никто не сыграл.")
    text = "\n".join(
        f"{n}. {escape(r[0])} — {r[1]} ({r[2]}/{r[3]})" for n, r in enumerate(rows, 1)
    )
    await m.answer("<b>Топ</b>\n" + text)


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
    await m.answer("Ищу соперника. /cancel — выйти из очереди.")


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
    sent = await m.answer(f"Стол: {escape(me[1])}. Нужен второй игрок.", reply_markup=kb)
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
        return await cb.answer("Ты уже за столом. Нужен второй игрок.")
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
    await cb.answer("Партия началась в личке бота.")
    if group_ref:
        await safe_edit(group_ref[0], group_ref[1], "Партия идёт.", None)


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
    arg = parts[3] if len(parts) > 3 else None
    sess = sessions.get(gid)
    if not sess:
        return await cb.answer("Партия окончена.", show_alert=True)
    if cb.from_user.id not in sess.uids:
        return await cb.answer()
    i = sess.uids.index(cb.from_user.id)
    g = sess.game

    async with sess.lock:
        if g.over:
            return await cb.answer("Партия окончена.")
        if action == "f":
            return await cb.answer(faces_table(), show_alert=True)
        if g.turn != i:
            return await cb.answer("Сейчас не твой ход.")

        before = g.decision_no
        alert = ""
        popup = ""

        if action == "x":
            sess.menu[i] = None
        elif action in ("t", "v"):
            sess.menu[i] = None
            res = g.take(i) if action == "t" else g.give(i)
            if not res.ok:
                popup = res.err
        elif action == "u":
            if arg in ("check", "swap", "copy"):
                ok, err = g.can_use(i, arg)
                if ok:
                    sess.menu[i] = arg
                else:
                    popup = err
            else:
                res = g.use(i, arg)
                if not res.ok:
                    popup = res.err
        elif action == "c":
            res = g.use(i, "check", int(arg))
            sess.menu[i] = None
            if res.ok:
                alert = res.private
            else:
                popup = res.err
        elif action == "s":
            res = g.use(i, "swap", int(arg))
            sess.menu[i] = None
            if not res.ok:
                popup = res.err
        elif action == "k":
            res = g.use(i, "copy", arg)
            sess.menu[i] = None
            if not res.ok:
                popup = res.err

        # подменю не должно переживать смену хода
        if g.turn != i or g.over:
            sess.menu = [None, None]

        await refresh(sess)
        if alert:
            await cb.answer(alert, show_alert=True)
        elif popup:
            await cb.answer(popup)
        else:
            await cb.answer()

        if g.over:
            await finish(sess)
        elif g.decision_no != before:
            schedule_timer(sess)


# ======================================================================
# Запуск
# ======================================================================

async def main() -> None:
    global BOT, BOT_USERNAME
    token = os.environ["BOT_TOKEN"]
    logging.basicConfig(level=logging.INFO)
    BOT = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await BOT.get_me()).username
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
