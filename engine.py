"""Движок партии. Только правила, никакого Telegram.

Матч: до двух выигранных смен. Жизней в сменах: 2 / 3 / 4.
Очередь позиций, у каждой позиции есть лицо. Одиночное лицо всегда оригинал.
Парные лица (одинаковые или близнецы): одна из двух позиций копия.
Ход: любое число предметов, затем Принять или Отдать.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

SHIFTS_TO_WIN = 2
HP_BY_SHIFT = {1: 2, 2: 3, 3: 4}
ITEMS_PER_CHARGE = {1: 0, 2: 2, 3: 3}
INVENTORY_CAP = 5
DISTORT_CHANCE = 0.25          # смена 3: шанс, что Сверка искажена
TWIN_CHANCE = 0.5              # смена 3: шанс, что пара состоит из близнецов
SIZE_RANGE = {1: (4, 6), 2: (5, 7), 3: (6, 8)}
PAIRS_RANGE = {1: (1, 2), 2: (2, 3), 3: (2, 4)}

# Семейство лиц: (вариант А, вариант Б). Близнецы это два варианта одного семейства.
FAMILIES = [
    ("◐", "◑"), ("◆", "◇"), ("▲", "△"), ("●", "○"),
    ("■", "□"), ("★", "☆"), ("◉", "◎"), ("▼", "▽"),
]

ITEM_NAMES = {
    "check": "Сверка",
    "swap": "Перестановка",
    "hold": "Задержка",
    "heal": "Перевязка",
    "copy": "Копирование",
    "noise": "Помеха",
    "void": "Невозможное",
}
ITEM_WEIGHTS = {
    "check": 3, "swap": 2, "hold": 2, "heal": 2,
    "copy": 1, "noise": 2, "void": 0.6,
}
ITEM_HELP = {
    "check": "Выбери пару лиц и узнай, какая из двух позиций копия. Ответ только тебе. Сопернику видно, какую пару ты сверял.",
    "swap": "Две соседние позиции в очереди меняются местами. Видно обоим.",
    "hold": "Соперник пропускает свой следующий ход.",
    "heal": "+1 жизнь, не выше максимума смены.",
    "copy": "Берёшь копию любого предмета соперника. У него предмет остаётся.",
    "noise": "На один ход соперника лица в очереди скрыты. Сверка и Перестановка ему недоступны.",
    "void": "Отменяет следующий удар по тебе, свой или чужой.",
}


@dataclass
class Slot:
    uid: int
    face: str
    copy: bool
    pair: int          # номер пары, -1 для одиночной позиции


@dataclass
class Item:
    kind: str
    distorted: bool = False


@dataclass
class Result:
    ok: bool
    err: str = ""
    private: str = ""


def make_charge(shift: int, rng: random.Random) -> list[Slot]:
    lo, hi = SIZE_RANGE[shift]
    n = rng.randint(lo, hi)
    plo, phi = PAIRS_RANGE[shift]
    k = min(rng.randint(plo, phi), n // 2)
    fams = rng.sample(FAMILIES, n - k)      # n-k <= 8 при любых параметрах
    slots: list[Slot] = []
    uid = 0
    for pair_id, (a, b) in enumerate(fams[:k]):
        twin = shift >= 3 and rng.random() < TWIN_CHANCE
        faces = (a, b) if twin else (a, a)
        copy_idx = rng.randint(0, 1)
        for j in (0, 1):
            slots.append(Slot(uid, faces[j], j == copy_idx, pair_id))
            uid += 1
    for fam in fams[k:]:
        slots.append(Slot(uid, rng.choice(fam), False, -1))
        uid += 1
    rng.shuffle(slots)
    return slots


class Game:
    def __init__(self, names, first: int | None = None, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.names = list(names)
        self.shifts = [0, 0]
        self.shift = 0
        self.charge_no = 0
        self.log: list[str] = []
        self.over = False
        self.winner: int | None = None
        self.decision_no = 0
        self.timeouts = [0, 0]
        self.slots: list[Slot] = []
        self.ptr = 0
        self.known: list[dict[int, bool]] = [{}, {}]
        if first is None:
            first = self.rng.randint(0, 1)
        self._start_shift(first)

    # ---------- служебное ----------

    def _say(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-12]

    def _start_shift(self, first: int) -> None:
        self.shift += 1
        self.max_hp = HP_BY_SHIFT[self.shift]
        self.hp = [self.max_hp, self.max_hp]
        self.items: list[list[Item]] = [[], []]      # предметы сгорают в конце смены
        self.skip = [False, False]
        self.blind = [False, False]
        self.shield = [False, False]
        self.turn = first
        self.charge_no = 0
        self._say(f"Смена {self.shift}. Первым ходит {self.names[first]}.")
        if self.shift == 3:
            self._say("Искажение: Сверка может ошибаться.")
        self._new_charge()

    def _roll_item(self, kind: str | None = None) -> Item:
        if kind is None:
            kinds = list(ITEM_WEIGHTS)
            kind = self.rng.choices(kinds, [ITEM_WEIGHTS[k] for k in kinds])[0]
        distorted = (
            self.shift == 3 and kind == "check" and self.rng.random() < DISTORT_CHANCE
        )
        return Item(kind, distorted)

    def _new_charge(self) -> None:
        self.charge_no += 1
        self.slots = make_charge(self.shift, self.rng)
        self.ptr = 0
        self.known = [{}, {}]
        copies = sum(1 for s in self.slots if s.copy)
        self._say(f"Заряд {self.charge_no}: позиций {len(self.slots)}, копий {copies}.")
        n = ITEMS_PER_CHARGE[self.shift]
        if n:
            for p in (0, 1):
                for _ in range(n):
                    if len(self.items[p]) >= INVENTORY_CAP:
                        break
                    self.items[p].append(self._roll_item())
            self._say("Выданы предметы.")

    def _hit(self, target: int) -> bool:
        """Наносит 1 урон. Возвращает True, если смена закончилась."""
        if self.shield[target]:
            self.shield[target] = False
            self._say("Невозможное: удар отменён.")
            return False
        self.hp[target] -= 1
        self._say(f"{self.names[target]} −1. Жизни: {self.hp[target]}/{self.max_hp}.")
        if self.hp[target] <= 0:
            self._end_shift(1 - target)
            return True
        return False

    def _end_shift(self, w: int) -> None:
        self.shifts[w] += 1
        self.timeouts = [0, 0]
        self._say(
            f"Смена {self.shift} за {self.names[w]}. Счёт {self.shifts[0]}:{self.shifts[1]}."
        )
        if self.shifts[w] >= SHIFTS_TO_WIN:
            self.over = True
            self.winner = w
            self._say(f"Матч за {self.names[w]}.")
            return
        self._start_shift(1 - w)        # проигравший смену ходит первым

    def _end_turn(self, i: int, keep: bool) -> None:
        if keep:
            self.turn = i
            return
        self.blind[i] = False
        nxt = 1 - i
        if self.skip[nxt]:
            self.skip[nxt] = False
            self._say(f"{self.names[nxt]} пропускает ход.")
            self.turn = i
        else:
            self.turn = nxt

    def _guard(self, i: int) -> str:
        if self.over:
            return "Партия окончена."
        if self.turn != i:
            return "Сейчас не твой ход."
        return ""

    # ---------- публичная информация ----------

    def next_status(self) -> str:
        """Что публично известно о следующей позиции: clean / copy / unknown."""
        s = self.slots[self.ptr]
        if s.pair < 0:
            return "clean"
        for j, t in enumerate(self.slots):
            if t.pair == s.pair and j != self.ptr and j < self.ptr:
                return "copy" if s.copy else "clean"
        return "unknown"

    def notes_for(self, i: int) -> list[str]:
        out = []
        for j in range(self.ptr, len(self.slots)):
            v = self.known[i].get(self.slots[j].uid)
            if v is not None:
                out.append(f"поз. {j + 1} — {'копия' if v else 'оригинал'}")
        return out

    # ---------- основные действия ----------

    def take(self, i: int) -> Result:
        return self._act(i, True)

    def give(self, i: int) -> Result:
        return self._act(i, False)

    def _act(self, i: int, to_self: bool, auto: bool = False) -> Result:
        err = self._guard(i)
        if err:
            return Result(False, err)
        self.decision_no += 1
        if not auto:
            self.timeouts[i] = 0
        slot = self.slots[self.ptr]
        self.ptr += 1
        target = i if to_self else 1 - i
        verb = "принимает" if to_self else "отдаёт"
        if slot.copy:
            self._say(f"{self.names[i]} {verb}. {slot.face} — копия.")
            if self._hit(target):
                return Result(True)
            keep = False
        else:
            self._say(f"{self.names[i]} {verb}. {slot.face} — оригинал.")
            keep = to_self
        self._end_turn(i, keep)
        if self.ptr >= len(self.slots):
            self._new_charge()
        return Result(True)

    def auto_move(self, i: int) -> None:
        if self.over or self.turn != i:
            return
        self.timeouts[i] += 1
        self._say(f"{self.names[i]}: время вышло.")
        if self.timeouts[i] >= 2:
            self.decision_no += 1
            self._say(f"{self.names[i]} проигрывает смену.")
            self._end_shift(1 - i)
            return
        self._act(i, self.rng.random() < 0.5, auto=True)

    def surrender(self, i: int) -> None:
        if self.over:
            return
        self.over = True
        self.winner = 1 - i
        self._say(f"{self.names[i]} сдаётся. Матч за {self.names[1 - i]}.")

    # ---------- предметы ----------

    def has(self, i: int, kind: str) -> bool:
        return any(it.kind == kind for it in self.items[i])

    def count(self, i: int, kind: str) -> int:
        return sum(1 for it in self.items[i] if it.kind == kind)

    def check_options(self) -> list[tuple[int, str]]:
        out = []
        for p in sorted({s.pair for s in self.slots if s.pair >= 0}):
            idxs = [j for j, s in enumerate(self.slots) if s.pair == p]
            if any(j >= self.ptr for j in idxs):
                out.append((p, "".join(self.slots[j].face for j in idxs)))
        return out

    def swap_options(self) -> list[int]:
        return list(range(max(0, len(self.slots) - self.ptr - 1)))

    def copy_options(self, i: int) -> list[str]:
        seen: list[str] = []
        for it in self.items[1 - i]:
            if it.kind not in seen:
                seen.append(it.kind)
        return seen

    def can_use(self, i: int, kind: str) -> tuple[bool, str]:
        err = self._guard(i)
        if err:
            return False, err
        if not self.has(i, kind):
            return False, "Нет такого предмета."
        o = 1 - i
        if kind in ("check", "swap") and self.blind[i]:
            return False, "Помеха: лица скрыты."
        if kind == "check" and not self.check_options():
            return False, "Нечего сверять."
        if kind == "swap" and not self.swap_options():
            return False, "Нечего переставлять."
        if kind == "hold" and self.skip[o]:
            return False, "Задержка уже наложена."
        if kind == "heal" and self.hp[i] >= self.max_hp:
            return False, "Жизни полные."
        if kind == "copy" and not self.items[o]:
            return False, "У соперника нет предметов."
        if kind == "noise" and self.blind[o]:
            return False, "Помеха уже наложена."
        if kind == "void" and self.shield[i]:
            return False, "Уже активно."
        return True, ""

    def use(self, i: int, kind: str, arg=None) -> Result:
        ok, err = self.can_use(i, kind)
        if not ok:
            return Result(False, err)
        o = 1 - i
        name = self.names[i]

        # проверка аргумента до списания предмета
        if kind == "check":
            opts = dict(self.check_options())
            if arg not in opts:
                return Result(False, "Такой пары нет.")
        elif kind == "swap":
            if arg not in self.swap_options():
                return Result(False, "Так переставить нельзя.")
        elif kind == "copy":
            if arg not in self.copy_options(i):
                return Result(False, "У соперника нет такого предмета.")

        idx = next(j for j, it in enumerate(self.items[i]) if it.kind == kind)
        item = self.items[i].pop(idx)
        private = ""

        if kind == "check":
            idxs = [j for j, s in enumerate(self.slots) if s.pair == arg]
            true_copy = next(j for j in idxs if self.slots[j].copy)
            other = next(j for j in idxs if j != true_copy)
            said_copy, said_clean = (other, true_copy) if item.distorted else (true_copy, other)
            self.known[i][self.slots[said_copy].uid] = True
            self.known[i][self.slots[said_clean].uid] = False
            label = opts[arg]
            self._say(f"{name}: Сверка {label}.")
            private = (
                f"Сверка {label}: копия — позиция {said_copy + 1}, "
                f"оригинал — позиция {said_clean + 1}."
            )
        elif kind == "swap":
            a = self.ptr + arg
            self.slots[a], self.slots[a + 1] = self.slots[a + 1], self.slots[a]
            self._say(f"{name}: Перестановка. Позиции {a + 1} и {a + 2} меняются местами.")
        elif kind == "hold":
            self.skip[o] = True
            self._say(f"{name}: Задержка. {self.names[o]} пропустит ход.")
        elif kind == "heal":
            self.hp[i] += 1
            self._say(f"{name}: Перевязка. Жизни: {self.hp[i]}/{self.max_hp}.")
        elif kind == "copy":
            self.items[i].append(self._roll_item(arg))
            self._say(f"{name}: Копирование. Взята копия: {ITEM_NAMES[arg]}.")
        elif kind == "noise":
            self.blind[o] = True
            self._say(f"{name}: Помеха. Лица скрыты у {self.names[o]} на один ход.")
        elif kind == "void":
            self.shield[i] = True
            self._say(f"{name}: Невозможное.")
        return Result(True, private=private)
