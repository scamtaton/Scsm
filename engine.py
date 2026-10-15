"""Игровая логика. Про Telegram здесь ничего не известно.

Суть. Двое за столом, между ними лоток с таблетками: чистые и горькие.
Состав лотка известен обоим и пересчитывается после каждой выдачи.

Выдача. Выдающий берёт из лотка таблетку, называет её («чистая» или «горькая»,
название может быть ложью) и протягивает второму. Второй решает:
  принять     горькая: −ставка ему; чистая: ничего.
  отказаться  таблетку вскрывают. Горькая: её принимает выдающий, −ставка ему.
              Чистая: отказ необоснован, −ставка отказавшемуся.
Потом роли меняются. Курс кончается, когда у одного рассудок на нуле.
Матч до двух курсов.

Что изменено по результатам разбора (см. комментарии «РАЗБОР»):
  1. Лоток убирается, как только в нём остался один вид таблеток. Иначе последняя
     таблетка выдаётся «на виду» и тот, кто её выдаёт, проигрывает по чётности лотка.
     Точный расчёт (минимакс): преимущество первого выдающего в курсе 1 было 70%,
     стало 55%.
  2. Предметы в курсе все разные, а Пункт 7 и Расписка вместе не подписываются
     (раньше связка «Фиксатор + Расписка» убивала с полного запаса за один ход).
  3. Таймаут выдающего: таблетка наугад, слово честное (раньше лотерея поверх лотереи).
  4. Использование предмета даёт игроку новое время (раньше таймер не сбрасывался).
  5. surrender() проверяет номер игрока. Примечания идут по общему кругу на весь бот.
"""
from __future__ import annotations

import math
import random
import time
import zlib
from dataclasses import dataclass

CLEAN, BITTER = "c", "b"
PILL_NAME = {CLEAN: "чистая", BITTER: "горькая"}
PILL_MARK = {CLEAN: "○", BITTER: "●"}

# Строки лога с этим префиксом бот рисует курсивом (примечания оператора, помехи).
NOTE_MARK = "§"

ITEM_NAMES = {
    "recorder": "Диктофон",
    "seal": "Пункт 7",
    "receipt": "Расписка",
    "carrot": "Морковь",
}
ITEM_WHO = {
    "recorder": "принимающему",
    "seal": "выдающему",
    "receipt": "выдающему",
    "carrot": "любому, в свой ход",
}
ITEM_HELP = {
    "recorder": "Включить запись голоса выдающего. Скажет, солгал он или нет. Решение остаётся за тобой.",
    "seal": "Ставится до выдачи. Отказ в этой выдаче не предусмотрен. С Распиской вместе не подписывается.",
    "receipt": "Ставится до выдачи. Ставка ×2: все потери в этой выдаче вдвое больше. Может закончить курс сразу.",
    "carrot": "Рассудок +1, но не выше максимума курса. Свежая. Откуда она здесь, не уточняется.",
}
ITEM_KEYS = list(ITEM_NAMES)

# курс: (рассудок, предметов каждому, размер лотка от, до)
# Предметы в курсе у игрока все разные (не больше одного каждого вида).
# Курс 3 на три единицы рассудка: решающему курсу нужно место для чтения соперника,
# и Расписка в нём уже не убивает с полного запаса. Вернуть как было: поставь 2 вместо 3.
ROUNDS = {
    1: (2, 0, 4, 4),
    2: (2, 2, 4, 6),
    3: (3, 3, 5, 6),
}
WINS_NEEDED = 2

# ----------------------------------------------------------------------
# Тексты. Ровный тон, без восклицаний. Одна странная деталь на строку, не больше.
# ----------------------------------------------------------------------

# Примечания оператора: один пункт на переход между курсами.
NOTES = [
    "Музыка за стеной не относится к процедуре.",
    "Если выдающий улыбается, это ничего не значит.",
    "Дверь не заперта. Это не отменяет согласия.",
    "Пауза перед ответом фиксируется.",
    "Если кажется, что эту таблетку уже принимали, так и есть.",
    "Не благодарите выдающего.",
    "Название таблетки не обязано совпадать с таблеткой.",
    "Лоток пересчитывать не нужно. Он уже пересчитан.",
    "Счёт времени ведётся на плёнке. Часы в помещении не учитываются.",
    "Вторая сторона тоже подписала согласие. Проверять это не нужно.",
    "Молчание фиксируется так же, как слова.",
    "Если выдающий моргает реже вас, не сообщайте ему об этом.",
    "Если вас окликнули по имени, не отвечайте сразу. Подождите, окликнут ещё раз.",
    "Участников должно быть двое. Если вам кажется, что их трое, считайте одного.",
    "Тени на стене принадлежат присутствующим. Если теней больше, сообщите оператору. Оператора нет.",
    "Не смотрите на выдающего дольше, чем он смотрит на вас.",
    "Если плёнка перематывается сама, не мешайте ей.",
    "Если таблетка тёплая, это ещё ничего не значит.",
    "Если голос за дверью совпадает с голосом участника, дверь не открывать. Она не заперта.",
    "Если выдающий улыбается шире, чем это возможно, он по-прежнему выдающий.",
    "Остаток предметов после курса не пересчитывается. Его уже пересчитали.",
    "Если лоток что-то произнёс, не повторяйте.",
    # немного мультфильма, строго по протоколу
    "Если выдающий спрашивает «Как дела, док?», это не выдающий. Продолжайте процедуру.",
    "Нарисованная на стене дверь не является выходом. Проверять не нужно.",
    "Динамит с надписью ACME не относится к таблеткам. Если выдающий настаивает, см. пункт 7.",
    "Морковь в лотке не числится. Если вы её видите, она ваша.",
]

# Помехи плёнки: показываются под панелью, зависят от состояния, у обоих игроков одинаковые.
ANOMALIES = [
    "Кадр 00:14 повторился. Никто не заметил.",
    "В углу кадра что-то стоит. В комнате этого нет.",
    "Звук на секунду опережает изображение.",
    "Лоток на плёнке чуть длиннее, чем на столе.",
    "Кто-то на записи моргнул. Это был не участник.",
    "Тень у двери не двигается вместе с остальными.",
    "Дверь на плёнке приоткрыта. В комнате закрыта.",
    "Счётчик плёнки остановился. Потом пошёл дальше, как будто ничего.",
    "Лампа гудит на ноте, которой не бывает.",
    "Слышно, как где-то перематывают чужую плёнку.",
    "В блестящих боках таблеток отражается кто-то третий.",
    "Помехи. Когда они уходят, стол стоит чуть ближе.",
    "Запись на секунду теряет цвет. Потом возвращает не тот.",
    # немного мультфильма
    "На секунду стол стал плоским, как нарисованный. Потом снова нет.",
    "На стене нарисована дверь. Вчера её не было.",
    "Где-то далеко свистит падающий предмет. Ничего не падает.",
    "В соседней комнате кто-то заикается. Договаривать за него не нужно.",
    "Из-за стены: «Как дела, док?». Голос слишком довольный.",
]
# Только для того, у кого остался один рассудок.
ANOMALIES_LOW = [
    "Кто-то повторяет твоё имя за стеной. Интонация не твоя.",
    "Таблетки на столе кажутся крупнее, чем были.",
    "Ты не помнишь, как сел за этот стол. Это не мешает.",
    "В комнате стало тише. Тебя это не удивило.",
    "Второй смотрит не на тебя, а чуть позади.",
]

SIGNOFFS = [
    "Это всё, ребята.",
    "Э-это всё, ре-ребята.",
    "Это всё, ребята. Кто-то продолжает говорить.",
]

# ----------------------------------------------------------------------
# Плёнка кончается. Декор, на правила игры не влияет.
# ----------------------------------------------------------------------

TAPE_SLOTS = 8          # длина шкалы «ОСТАТОК» на панели
TAPE_FULL, TAPE_USED = "█", "░"

# Показывается вместо случайной помехи, когда на шкале остался последний сегмент.
TAPE_LOW = [
    "Плёнка на исходе. Дальше запись идёт поверх того, что было записано раньше.",
    "Остаток плёнки: последний сегмент. Шипение стало громче.",
    "Плёнка почти закончилась. Если она закончится, запись не остановится.",
]

# Финальный кадр партии: конец плёнки.
TAPE_ENDS = [
    "▒▒▒ ПЛЁНКА ЗАКОНЧИЛАСЬ ▒▒▒\nДальше пусто. Не перематывайте.",
    "КОНЕЦ ПЛЁНКИ\nЩёлк. Тишина. Щёлк.",
    "▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒\nПлёнка закончилась. Запись нет.",
    "[ПЛЁНКА ЗАКОНЧИЛАСЬ]\nЕсли шипение продолжается, это не плёнка.",
    "КОНЕЦ ЗАПИСИ\nЛента крутится вхолостую. Кто-то ещё слушает.",
    "ЩЁЛК.\nПлёнка закончилась. Лампа записи ещё горит.",
]

# Оборванные на полуслове строки: хвосты для экранов вне партии
# (как будто плёнка кончилась прямо во время фразы).
CUTS = [
    "Если вы читаете это, плёнка ещё",
    "…и тогда он сказал, что дверь на самом деле",
    "Запись продолжается. Запись про▒▒▒▒▒▒",
    "Не оборачивайтесь, пока он не",
    "Участников должно быть двое. Третий не",
    "Согласие действует до конца записи. Конец записи наступает, когда",
    "Если на этом месте стало тихо, значит",
    "▒▒▒▒▒▒ ▒▒▒▒ ▒▒▒▒▒▒▒▒. Дальше не разобрать.",
    "Следующий пункт каталога утерян. Он начинался с",
    "Оператор просил передать, что",
    "…кто-то перематывает плёнку на то место, где вы",
    "Проверьте, сколько вас. Если получилось двое, не",
]

# Заголовки-штампы: «инвентарный номер» экрана. Номер берётся из строки-ключа,
# поэтому у одного и того же экрана он всегда одинаковый.
STAMPS = [
    "ИНВ. №{n:04d} · КОПИЯ · НЕ ВЫНОСИТЬ",
    "КАТАЛОГ · ПОЗИЦИЯ №{n:04d}",
    "ЗАПИСЬ №{n:04d} · КОПИЯ ПОВРЕЖДЕНА",
    "ПЛЁНКА №{n:04d} · ХРАНЕНИЕ: ЗАКРЫТОЕ",
]


def stamp(key: str) -> str:
    """Штамп каталога для экрана key. Один и тот же ключ даёт один и тот же штамп."""
    h = zlib.crc32(key.encode())
    return STAMPS[h % len(STAMPS)].format(n=h % 9000 + 1000)


def cut_line(rng: random.Random | None = None) -> str:
    return (rng or random).choice(CUTS)

# Варианты строк лога. Поля: {s} ставка, {p} игрок, {n}/{b}/{c} лоток, {k} остаток, {kind} вид.
LINES = {
    "tb": [  # приняли горькую
        "Приём. Рассудок −{s}: {p}.",
        "Приём. Вкус запомнится. Рассудок −{s}: {p}.",
        "Приём. Горечь приходит не сразу. Рассудок −{s}: {p}.",
    ],
    "tc": [  # приняли чистую
        "Приём. Без последствий.",
        "Приём. Ничего. Это тоже фиксируется.",
        "Приём. Ничего не произошло. Пока.",
    ],
    "rb": [  # отказались от горькой
        "Отказ. Принимает выдающий. Рассудок −{s}: {p}.",
        "Отказ. Таблетка возвращается тому, кто её протянул. Рассудок −{s}: {p}.",
        "Отказ. Вскрыто при свидетелях. Рассудок −{s}: {p}.",
    ],
    "rc": [  # отказались от чистой
        "Отказ необоснован. Рассудок −{s}: {p}.",
        "Отказ необоснован. Таблетка была в порядке. Рассудок −{s}: {p}.",
        "Отказ необоснован. Недоверие тоже расходуется. Рассудок −{s}: {p}.",
    ],
    "load_empty": [
        "Лоток пуст. Заряжен новый: {n} шт. Горьких {b}, чистых {c}.",
        "Лоток пуст. Его зарядили, пока вы не смотрели: {n} шт. Горьких {b}, чистых {c}.",
    ],
    "load_mono": [
        "Остались только {kind} ({k}). Выбора нет, лоток убран. Новый: {n} шт. Горьких {b}, чистых {c}.",
        "В лотке только {kind} ({k}). Это не выбор. Лоток заменён: {n} шт. Горьких {b}, чистых {c}.",
    ],
    "timeout_take": [
        "Время вышло ({p}). Решение принято автоматически.",
        "Пауза затянулась ({p}). Молчание считается согласием.",
    ],
    "carrot": [
        "Хруст слышно в соседней комнате.",
        "Свежая. В лотке её не было.",
        "Откуда она, не уточняется.",
    ],
}

MONTHS = ("СЕН", "ОКТ", "НОЯ", "ДЕК")

# Круг примечаний общий на весь процесс: игрок не увидит одну и ту же строку
# на двух курсах подряд и по возможности не повторит её между партиями.
_NOTE_BAG: list[str] = []
_NOTE_LAST: str | None = None


def next_note(rng: random.Random) -> str:
    global _NOTE_LAST
    if not _NOTE_BAG:
        bag = NOTES[:]
        rng.shuffle(bag)
        if _NOTE_LAST is not None and bag[-1] == _NOTE_LAST:   # pop() берёт с конца
            bag[0], bag[-1] = bag[-1], bag[0]
        _NOTE_BAG.extend(bag)
    _NOTE_LAST = _NOTE_BAG.pop()
    return _NOTE_LAST


def P(i: int) -> str:
    """Плейсхолдер игрока в строке лога. Подставляется при отрисовке: «ты» или имя."""
    return "{" + str(i) + "}"


@dataclass
class Offer:
    pill: str       # что на самом деле
    label: str      # что назвал выдающий
    stake: int      # ставка выдачи
    forced: bool    # Пункт 7: отказаться нельзя
    think: int      # сколько секунд выдающий выбирал
    auto: bool = False   # выдача по таймеру: таблетка наугад, слово честное


@dataclass
class Exchange:
    """Вскрытая выдача. Нужна боту-сопернику, чтобы подстраиваться под привычки человека."""
    dealer: int
    pill: str
    label: str
    take: bool
    auto: bool       # выдача по таймеру
    forced: bool     # Пункт 7


@dataclass
class Result:
    ok: bool
    err: str = ""
    private: str = ""


class Game:
    def __init__(self, names, first: int | None = None, rng: random.Random | None = None,
                 clock=time.monotonic):
        self.rng = rng or random.Random()
        self.clock = clock
        self.names = list(names)

        self.wins = [0, 0]
        self.round_no = 0
        self.charges = [0, 0]        # рассудок
        self.max_charges = 0
        self.items: list[list[str]] = [[], []]
        self.tray = {CLEAN: 0, BITTER: 0}

        self.dealer = first if first in (0, 1) else self.rng.randint(0, 1)
        self.phase = "deal"          # deal | decide | over
        self.offer: Offer | None = None
        self.stake = 1
        self.forced = False
        self.lied: bool | None = None    # что сказал Диктофон принимающему: солгал ли выдающий

        self.timeouts = [0, 0]
        self.exchange_no = 0
        self.decision_no = 0         # растёт при каждой новой точке выбора и при каждом предмете: нужен таймеру
        self.over = False
        self.winner: int | None = None
        self.log: list[str] = []
        self.history: list[Exchange] = []     # все вскрытые выдачи матча
        self._last_variant: dict[str, str] = {}

        self.started = self.t0 = self.clock()
        self.tape_no = f"{self.rng.randint(1, 999):04d}"
        self.tape_date = f"{self.rng.randint(1, 28)} {self.rng.choice(MONTHS)} 98"
        self._tape_base = self.rng.randint(21 * 3600, 23 * 3600 + 3000)

        self.log.append("Согласие подписано обеими сторонами.")
        self._start_round(1, self.dealer)

    # ------------------------------------------------------------------
    # Состояние для отрисовки
    # ------------------------------------------------------------------

    @property
    def turn(self) -> int:
        """Чьего действия ждём."""
        return self.dealer if self.phase == "deal" else 1 - self.dealer

    def pills(self) -> list[str]:
        return [k for k in (CLEAN, BITTER) if self.tray[k] > 0]

    def count(self, p: int, kind: str) -> int:
        return self.items[p].count(kind)

    def tray_total(self) -> int:
        return self.tray[CLEAN] + self.tray[BITTER]

    def tape_clock(self) -> str:
        t = (self._tape_base + int(self.clock() - self.started)) % 86400
        return f"{t // 3600:02d}:{t % 3600 // 60:02d}"

    def tape_left(self) -> int:
        """Сколько сегментов плёнки осталось (декор). Не меньше 1, пока партия идёт."""
        if self.over:
            return 0
        return max(1, TAPE_SLOTS - self.exchange_no * 2 // 5)   # ~0.4 сегмента за выдачу: пустеет к концу длинной партии

    def tape_bar(self) -> str:
        left = self.tape_left()
        return TAPE_FULL * left + TAPE_USED * (TAPE_SLOTS - left)

    def tape_end(self) -> str:
        """Финальный кадр: плёнка закончилась. Один и тот же для обоих игроков."""
        rnd = random.Random(zlib.crc32(f"{self.tape_no}:tapeend".encode()))
        return rnd.choice(TAPE_ENDS)

    def anomaly(self, viewer: int) -> str | None:
        """Помеха плёнки для этого кадра или None. Зависит только от состояния,
        поэтому перерисовка не меняет картинку, а оба игрока видят одно и то же
        (кроме строк для того, у кого остался один рассудок)."""
        if self.over:
            return None
        if self.tape_left() <= 1:
            return TAPE_LOW[self.exchange_no % len(TAPE_LOW)]
        low = self.max_charges >= 2 and self.charges[viewer] == 1
        seed = f"{self.tape_no}:{self.exchange_no}:{self.phase}:{int(low)}"
        rnd = random.Random(zlib.crc32(seed.encode()))
        chance = {1: 0.06, 2: 0.12, 3: 0.20}.get(self.round_no, 0.2) + (0.12 if low else 0.0)
        if rnd.random() >= chance:
            return None
        pool = ANOMALIES_LOW if (low and rnd.random() < 0.6) else ANOMALIES
        return rnd.choice(pool)

    def signoff(self) -> str:
        rnd = random.Random(zlib.crc32(f"{self.tape_no}:end".encode()))
        return rnd.choice(SIGNOFFS)

    # ------------------------------------------------------------------
    # Курсы и лоток
    # ------------------------------------------------------------------

    def _say(self, key: str, **kw) -> str:
        """Случайный вариант строки, но не тот же, что в прошлый раз."""
        options = LINES[key]
        last = self._last_variant.get(key)
        pool = [o for o in options if o != last] or options
        choice = self.rng.choice(pool)
        self._last_variant[key] = choice
        return choice.format(**kw)

    def _start_round(self, n: int, dealer: int) -> None:
        charges, n_items, _, _ = ROUNDS[n]
        self.round_no = n
        self.max_charges = charges
        self.charges = [charges, charges]
        self.items = [self.rng.sample(ITEM_KEYS, n_items) for _ in (0, 1)]
        self.dealer = dealer
        self.timeouts = [0, 0]
        tail = {1: "", 2: "", 3: " Следующего курса нет."}[n]
        self.log.append(
            f"КУРС {n}. Рассудок: {charges}. "
            + ("Предметы выданы." if n_items else "Предметы не выдаются.")
            + tail
        )
        self._load_tray("start")
        self.log.append(f"Первая выдача: {P(dealer)}.")
        self._begin_deal()

    def _load_tray(self, why: str, leftover: tuple[str, int] | None = None) -> None:
        _, _, lo, hi = ROUNDS[self.round_no]
        n = self.rng.randint(lo, hi)
        b = self.rng.randint(max(1, math.ceil(n * 0.3)), min(n - 1, math.floor(n * 0.6)))
        self.tray = {BITTER: b, CLEAN: n - b}
        c = n - b
        if why == "start":
            self.log.append(f"Лоток заряжен: {n} шт. Горьких {b}, чистых {c}.")
        elif why == "empty":
            self.log.append(self._say("load_empty", n=n, b=b, c=c))
        else:
            kind, k = leftover
            word = "горькие" if kind == BITTER else "чистые"
            self.log.append(self._say("load_mono", kind=word, k=k, n=n, b=b, c=c))

    def _maybe_reload(self) -> None:
        # РАЗБОР: лоток убирается, когда остался один вид. Выдача из лотка, где выбора
        # уже нет, это не блеф, а ловушка с известным исходом; она доставалась тому,
        # кто выдаёт последним по чётности, и давала первому выдающему +20% на курсе 1.
        c, b = self.tray[CLEAN], self.tray[BITTER]
        if c and b:
            return
        if c + b == 0:
            self._load_tray("empty")
        else:
            self._load_tray("mono", (CLEAN if c else BITTER, c + b))

    def _begin_deal(self) -> None:
        self.phase = "deal"
        self.offer = None
        self.stake = 1
        self.forced = False
        self.lied = None
        self.t0 = self.clock()
        self.decision_no += 1

    def _end_round(self, w: int) -> None:
        self.wins[w] += 1
        self.log.append(f"КУРС {self.round_no} закрыт. Победа: {P(w)}.")
        if self.wins[w] >= WINS_NEEDED:
            self._finish(w)
            return
        self.log.append(NOTE_MARK + next_note(self.rng))
        self._start_round(self.round_no + 1, 1 - w)   # проигравший курс выдаёт первым

    def _finish(self, w: int) -> None:
        self.over = True
        self.winner = w
        self.phase = "over"
        self.offer = None

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------

    def deal(self, i: int, pill: str, label: str) -> Result:
        if self.over or self.phase != "deal" or i != self.dealer:
            return Result(False, "Сейчас не твоя выдача.")
        if pill not in (CLEAN, BITTER) or label not in (CLEAN, BITTER):
            return Result(False, "Неверный выбор.")
        if self.tray[pill] <= 0:
            return Result(False, "Таких таблеток в лотке нет.")
        self.timeouts[i] = 0
        self._offer(pill, label)
        return Result(True)

    def _offer(self, pill: str, label: str, auto: bool = False) -> None:
        think = round(self.clock() - self.t0)
        self.offer = Offer(pill, label, self.stake, self.forced, think, auto)
        self.lied = None
        self.phase = "decide"
        self.t0 = self.clock()
        self.decision_no += 1

    def answer(self, i: int, take: bool) -> Result:
        if self.over or self.phase != "decide" or i != self.turn:
            return Result(False, "Сейчас не твоё решение.")
        if not take and self.offer.forced:
            return Result(False, "Пункт 7: отказаться нельзя.")
        self.timeouts[i] = 0
        self._resolve(take)
        return Result(True)

    def _resolve(self, take: bool) -> None:
        o = self.offer
        d, r, s = self.dealer, 1 - self.dealer, o.stake
        head = f"{self.exchange_no + 1}. Названо «{PILL_NAME[o.label]}», была {PILL_NAME[o.pill]}. "
        hit = None
        if take:
            if o.pill == BITTER:
                hit, text = r, self._say("tb", s=s, p=P(r))
            else:
                text = self._say("tc")
        else:
            if o.pill == BITTER:
                hit, text = d, self._say("rb", s=s, p=P(d))
            else:
                hit, text = r, self._say("rc", s=s, p=P(r))
        self.log.append(head + text)
        self.history.append(Exchange(d, o.pill, o.label, take, o.auto, o.forced))
        self.tray[o.pill] -= 1
        self.exchange_no += 1

        if hit is not None:
            self.charges[hit] = max(0, self.charges[hit] - s)
            if self.charges[hit] == 0:
                self._end_round(1 - hit)
                return

        self.dealer = 1 - self.dealer
        self._maybe_reload()
        self._begin_deal()

    # ------------------------------------------------------------------
    # Предметы
    # ------------------------------------------------------------------

    def can_use(self, i: int, kind: str) -> tuple[bool, str]:
        if self.over:
            return False, "Партия окончена."
        if i != self.turn:
            return False, "Предметы только в свой ход."
        if self.count(i, kind) == 0:
            return False, "Такого предмета нет."
        if kind == "recorder":
            if self.phase != "decide":
                return False, "Диктофон нужен, когда таблетку уже протянули."
            if self.lied is not None:
                return False, "Запись уже прослушана."
        elif kind == "seal":
            if self.phase != "deal":
                return False, "Пункт 7 ставится до выдачи."
            if self.forced:
                return False, "Пункт 7 уже стоит."
            if self.stake == 2:
                return False, "Пункт 7 и Расписка вместе не подписываются."
        elif kind == "receipt":
            if self.phase != "deal":
                return False, "Расписка подписывается до выдачи."
            if self.stake == 2:
                return False, "Расписка уже подписана."
            if self.forced:
                return False, "Пункт 7 и Расписка вместе не подписываются."
        elif kind == "carrot":
            if self.charges[i] >= self.max_charges:
                return False, "Рассудок полный."
        return True, ""

    def use(self, i: int, kind: str) -> Result:
        ok, err = self.can_use(i, kind)
        if not ok:
            return Result(False, err)
        self.items[i].remove(kind)
        # РАЗБОР: предмет даёт игроку новое время. Раньше два предмета съедали
        # 25 из 30 секунд, и на саму выдачу оставалось 5.
        self.decision_no += 1
        if kind == "recorder":
            self.lied = self.offer.pill != self.offer.label
            self.log.append(f"Диктофон: {P(i)}. Запись включена.")
            return Result(True, private=self.recorder_text())
        if kind == "seal":
            self.forced = True
            self.log.append(f"Пункт 7: {P(i)}. Отказ в этой выдаче не предусмотрен.")
        elif kind == "receipt":
            self.stake = 2
            self.log.append(f"Расписка: {P(i)}. Ставка ×2. Подписано обеими сторонами.")
        elif kind == "carrot":
            self.charges[i] += 1
            self.log.append(f"Морковь: {P(i)}. Рассудок +1. " + self._say("carrot"))
        return Result(True)

    def recorder_text(self) -> str:
        if self.lied is None:
            return ""
        return "Диктофон: голос дрогнул. Ложь." if self.lied else "Диктофон: голос ровный. Правда."

    # ------------------------------------------------------------------
    # Таймер и сдача
    # ------------------------------------------------------------------

    def auto_move(self, i: int) -> None:
        """Время вышло. Молчание принимающего считается согласием.
        Выдающий не успел: таблетка наугад из лотка, слово честное.
        Два раза подряд: курс проигран."""
        if self.over or i not in (0, 1) or i != self.turn:
            return
        self.timeouts[i] += 1
        if self.timeouts[i] >= 2:
            self.log.append(
                f"Время вышло дважды подряд ({P(i)}). Молчание тоже ответ. Курс засчитан другой стороне."
            )
            self.charges[i] = 0
            self._end_round(1 - i)
            return
        if self.phase == "deal":
            # РАЗБОР: раньше слово тоже было случайным, и таймаут мог подписать выдающего
            # под ложью, о которой он не знал. Теперь неудачи две: наугад и на виду.
            self.log.append(
                f"Время вышло ({P(i)}). Выдача автоматическая: таблетка наугад, слово честное."
            )
            bag = [CLEAN] * self.tray[CLEAN] + [BITTER] * self.tray[BITTER]
            pill = self.rng.choice(bag)
            self._offer(pill, pill, auto=True)
        else:
            self.log.append(self._say("timeout_take", p=P(i)))
            self._resolve(True)

    def surrender(self, i: int) -> None:
        if self.over or i not in (0, 1):
            return
        self.log.append(f"Досрочный выход: {P(i)}. Согласие остаётся в силе.")
        self._finish(1 - i)


# ======================================================================
# Соперник-бот. Нужен, чтобы тестировать партию в одиночку.
# Видит только то, что видел бы человек: лоток, свои предметы, названное слово,
# число предметов соперника и вскрытую историю выдач. Настоящую таблетку,
# которую ему протянули, не подсматривает (знает её только после Диктофона).
# ======================================================================

_BOT_RNG = random.Random()


def _value(my: int, op: int) -> float:
    """Оценка положения в курсе: разница рассудка; смерть соперника — много плюса, своя — ещё больше минуса."""
    my, op = max(my, 0), max(op, 0)
    v = float(my - op)
    if op == 0:
        v += 4
    if my == 0:
        v -= 6
    return v


def _habits(g: Game, me: int) -> dict:
    """Привычки соперника по вскрытым выдачам (с поправкой на малое число данных)."""
    opp = 1 - me
    lies = {CLEAN: [0, 0], BITTER: [0, 0]}      # по настоящей таблетке: [соврал, выдач]
    refused = {CLEAN: [0, 0], BITTER: [0, 0]}   # по названному слову: [отказов, выдач]
    for x in g.history:
        if x.dealer == opp and not x.auto:
            lies[x.pill][1] += 1
            lies[x.pill][0] += x.label != x.pill
        elif x.dealer == me and not x.forced:
            refused[x.label][1] += 1
            refused[x.label][0] += not x.take

    def rate(pair: list, prior: float, w: float = 2.0) -> float:
        return (pair[0] + prior * w) / (pair[1] + w)

    return {
        "lie": {BITTER: rate(lies[BITTER], 0.6), CLEAN: rate(lies[CLEAN], 0.25)},
        "refuse": {CLEAN: rate(refused[CLEAN], 0.25), BITTER: rate(refused[BITTER], 0.55)},
    }


def _p_bitter(g: Game, label: str, lie: dict) -> float:
    """Вероятность, что протянутая таблетка горькая, если названо label (байесовски по лотку и привычкам)."""
    nb, nc = g.tray[BITTER], g.tray[CLEAN]     # выданная ещё числится в лотке
    tot = nb + nc
    if tot == 0:
        return 0.5
    pb, pc = nb / tot, nc / tot
    say_b = (1 - lie[BITTER]) if label == BITTER else lie[BITTER]      # P(назовёт label | горькая)
    say_c = lie[CLEAN] if label == BITTER else (1 - lie[CLEAN])        # P(назовёт label | чистая)
    den = pb * say_b + pc * say_c
    return pb * say_b / den if den > 0 else 0.5


def bot_choose(g: Game, me: int, rng: random.Random | None = None) -> tuple:
    """Следующий шаг бота: ('use', предмет) | ('deal', таблетка, слово) | ('answer', принять?)."""
    rng = rng or _BOT_RNG
    opp = 1 - me
    my, op = g.charges[me], g.charges[opp]

    # Морковь: предметы к концу курса сгорают, так что берём её, как только есть куда добавлять.
    if g.can_use(me, "carrot")[0]:
        return ("use", "carrot")

    hab = _habits(g, me)

    # ---------------- решение: принять или отказаться ----------------
    if g.phase == "decide":
        o = g.offer
        if o.forced:
            return ("answer", True)
        if g.can_use(me, "recorder")[0]:
            return ("use", "recorder")
        if g.lied is None:
            q = _p_bitter(g, o.label, hab["lie"])
        else:   # Диктофон сказал, солгал ли выдающий: значит, таблетка известна точно
            q = 1.0 if ((o.label == BITTER) != g.lied) else 0.0
        s = o.stake
        ev_take = q * _value(my - s, op) + (1 - q) * _value(my, op)
        ev_refuse = q * _value(my, op - s) + (1 - q) * _value(my - s, op)
        k = 2.5 if 0 < q < 1 else 60.0           # при неуверенности чуть случайности, при точном знании нет
        z = max(-30.0, min(30.0, (ev_refuse - ev_take) * k))
        p_refuse = 1 / (1 + math.exp(-z))
        return ("answer", rng.random() >= p_refuse)

    # ---------------- выдача: что протянуть и что сказать ----------------
    pills = g.pills()

    def scored(stake: int, forced: bool) -> list[tuple[float, str, str]]:
        out = []
        for pill in pills:
            for label in (CLEAN, BITTER):
                r = 0.0 if forced else hab["refuse"][label]     # шанс, что человек откажется
                if pill == BITTER:      # принял: ему −ставка; отказался: мне −ставка
                    ev = (1 - r) * _value(my, op - stake) + r * _value(my - stake, op)
                else:                   # принял: ничего; отказался зря: ему −ставка
                    ev = (1 - r) * _value(my, op) + r * _value(my, op - stake)
                out.append((ev, pill, label))
        return out

    base = scored(g.stake, g.forced)
    plans = [(max(x[0] for x in base), None)]
    for kind, stake, forced in (("receipt", 2, False), ("seal", g.stake, True)):
        if g.can_use(me, kind)[0]:
            plans.append((max(x[0] for x in scored(stake, forced)) - 0.25, kind))   # небольшая плата за предмет
    _, kind = max(plans, key=lambda t: t[0])
    if kind:
        return ("use", kind)

    top = max(x[0] for x in base)
    weights = [math.exp(2.0 * (x[0] - top)) for x in base]      # лучшие варианты чаще, но не всегда
    _, pill, label = rng.choices(base, weights=weights)[0]
    return ("deal", pill, label)


def bot_move(g: Game, me: int, rng: random.Random | None = None) -> Result:
    """Выбирает и выполняет один шаг бота."""
    act = bot_choose(g, me, rng)
    if act[0] == "use":
        res = g.use(me, act[1])
    elif act[0] == "deal":
        res = g.deal(me, act[1], act[2])
    else:
        res = g.answer(me, act[1])
    if not res.ok:      # не должно случаться; страховка, чтобы партия не встала
        if g.phase == "deal" and g.turn == me:
            pill = g.pills()[0]
            res = g.deal(me, pill, pill)
        elif g.phase == "decide" and g.turn == me:
            res = g.answer(me, True)
    return res
