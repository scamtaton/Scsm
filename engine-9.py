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
from dataclasses import asdict, dataclass

import voice

CLEAN, BITTER = "c", "b"
PILL_NAME = {CLEAN: "чистая", BITTER: "горькая"}
PILL_MARK = {CLEAN: "○", BITTER: "●"}

# Строки лога с этим префиксом бот рисует курсивом (примечания оператора, помехи).
NOTE_MARK = "§"

# №05 «вырезанный пункт»: в каталоге его нет, имя и действие скрыты, пока не применён.
# Выдаётся редко (CUT_CHANCE), только с курса CUT_FROM_ROUND, вместо одного из обычных предметов.
CUT_KEY = "cut"
CUT_REAL = "Вырезанный пункт"
CUT_CHANCE = 0.18
CUT_FROM_ROUND = 3

ITEM_NAMES = {
    "recorder": "Диктофон",
    "seal": "Пункт 7",
    "receipt": "Расписка",
    "carrot": "Морковь",
    CUT_KEY: "▒▒▒▒▒▒",
}
ITEM_WHO = {
    "recorder": "принимающему",
    "seal": "выдающему",
    "receipt": "выдающему",
    "carrot": "любому, в свой ход",
    CUT_KEY: "▒▒▒▒▒▒",
}
ITEM_HELP = {
    "recorder": "Включить запись голоса выдающего. Скажет, солгал он или нет. Решение остаётся за тобой.",
    "seal": "Ставится до выдачи. Отказ в этой выдаче не предусмотрен. С Распиской вместе не подписывается.",
    "receipt": "Ставится до выдачи. Ставка ×2: все потери в этой выдаче вдвое больше. Может закончить курс сразу.",
    "carrot": "Рассудок +1, но не выше максимума курса. Свежая. Откуда она здесь, не уточняется.",
    CUT_KEY: "[ДАННЫЕ УДАЛЕНЫ]. Выдаётся редко и не всем. Что он делает, становится ясно после применения.",
}
# что на самом деле делает №05 (в каталоге этого нет; открывается в архиве после применения)
CUT_REAL_HELP = (
    "Применяется, когда таблетку уже протянули. Последствия этой выдачи вырезаются: "
    "никто ничего не теряет. Таблетку всё равно вскрывают при свидетелях."
)
ITEM_REAL = {**ITEM_NAMES, CUT_KEY: CUT_REAL}
ITEM_REAL_HELP = {**ITEM_HELP, CUT_KEY: CUT_REAL_HELP}
ITEM_KEYS = [k for k in ITEM_NAMES if k != CUT_KEY]      # обычная раздача; №05 идёт отдельным броском

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

# Строка в журнал партии, когда запись вернули после перерыва (/restore).
RESTORE_NOTES = [
    "Запись прервана и возобновлена. Время между кадрами не засчитывается.",
    "Плёнка перемотана на последний сохранённый кадр. То, что было после, не вернуть.",
    "Связь восстановлена. Если вам кажется, что вы это уже слышали, так и есть.",
    "Передача возобновлена. Участники те же. Это проверялось.",
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


# ----------------------------------------------------------------------
# Архив: всё, что игрок мог увидеть в партиях. Нужен /archive; на правила не влияет.
# ----------------------------------------------------------------------

ARCHIVE_GROUPS = {
    "notes": "Примечания оператора",
    "glitches": "Помехи плёнки",
    "ends": "Концы плёнки",
    "items": "Предметы",
}


def _eid(group: str, text: str) -> str:
    """Стабильный id записи: группа + контрольная сумма текста."""
    return f"{group[0]}{zlib.crc32(text.encode()):08x}"


def _flat(text: str) -> str:
    return text.replace("\n", " ")


def _build_archive() -> dict[str, tuple[str, str]]:
    cat: dict[str, tuple[str, str]] = {}
    for t in NOTES:
        cat[_eid("notes", t)] = ("notes", t)
    for t in ANOMALIES + ANOMALIES_LOW + TAPE_LOW:
        cat[_eid("glitches", t)] = ("glitches", t)
    for t in TAPE_ENDS + SIGNOFFS:
        cat[_eid("ends", _flat(t))] = ("ends", _flat(t))
    for n, kind in enumerate(ITEM_NAMES, 1):
        cat[f"i:{kind}"] = ("items", f"№{n:02d} · {ITEM_REAL[kind]}. {ITEM_REAL_HELP[kind]}")
    return cat


ARCHIVE: dict[str, tuple[str, str]] = _build_archive()

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
    "void": [  # последствия вырезаны (№05)
        "Последствий нет. Они вырезаны.",
        "Последствий нет. На плёнке на этом месте склейка.",
        "Последствий нет. Кадр вырезан, звук остался.",
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


def note_entry(*parts: str | None) -> str:
    """Одна запись журнала с примечанием оператора. Несколько фраз (реплика оператора и примечание курса)
    идут одной записью через перевод строки: бот показывает последние 6 записей, и на стыке курсов
    вторая запись вытолкнула бы строку с итогом выдачи."""
    return NOTE_MARK + "\n".join(p for p in parts if p)


def note_ids(line: str) -> list[str]:
    """id записей архива в строке журнала с NOTE_MARK (в одной записи может быть несколько фраз)."""
    return [_eid("notes", part) for part in line[len(NOTE_MARK):].split("\n")]


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
        self.used: set[str] = set()          # какие предметы уже применялись в матче (для архива)

        self.started = self.t0 = self.clock()
        self.tape_no = f"{self.rng.randint(1, 999):04d}"
        self.tape_date = f"{self.rng.randint(1, 28)} {self.rng.choice(MONTHS)} 98"
        self._tape_base = self.rng.randint(21 * 3600, 23 * 3600 + 3000)
        self.op = voice.OpState(2, f"{self.tape_no}:{self.tape_date}")     # память оператора (voice.py)

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

    def finds(self, viewer: int) -> list[str]:
        """id записей архива, которые игрок viewer сейчас видит на экране."""
        out: list[str] = []
        for line in self.log[-6:]:
            if line.startswith(NOTE_MARK):
                out += note_ids(line)
        a = self.anomaly(viewer)
        if a:
            out.append(_eid("glitches", a))
        if self.over:
            out.append(_eid("ends", _flat(self.tape_end())))
            out.append(_eid("ends", self.signoff()))
        out += [f"i:{k}" for k in sorted(self.used)]
        return [e for e in out if e in ARCHIVE]

    # ------------------------------------------------------------------
    # Сохранение и восстановление (для /restore). Время и счётчики таймеров не хранятся.
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        ver, internal, gauss = self.rng.getstate()
        return {
            "rng": [ver, list(internal), gauss],
            "names": list(self.names), "wins": list(self.wins), "round_no": self.round_no,
            "charges": list(self.charges), "max_charges": self.max_charges,
            "items": [list(x) for x in self.items], "tray": dict(self.tray),
            "dealer": self.dealer, "phase": self.phase,
            "offer": asdict(self.offer) if self.offer else None,
            "stake": self.stake, "forced": self.forced, "lied": self.lied,
            "timeouts": list(self.timeouts), "exchange_no": self.exchange_no,
            "decision_no": self.decision_no, "over": self.over, "winner": self.winner,
            "log": list(self.log), "history": [asdict(x) for x in self.history],
            "last_variant": dict(self._last_variant), "used": sorted(self.used),
            "age": self.clock() - self.started,
            "tape_no": self.tape_no, "tape_date": self.tape_date, "tape_base": self._tape_base,
            "op": self.op.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict, clock=time.monotonic) -> "Game":
        g = cls.__new__(cls)         # без __init__: он начинает новый курс
        now = clock()
        g.clock = clock
        g.rng = random.Random()
        ver, internal, gauss = d["rng"]
        g.rng.setstate((ver, tuple(internal), gauss))
        g.names = list(d["names"]); g.wins = list(d["wins"]); g.round_no = d["round_no"]
        g.charges = list(d["charges"]); g.max_charges = d["max_charges"]
        g.items = [list(x) for x in d["items"]]
        g.tray = {CLEAN: d["tray"][CLEAN], BITTER: d["tray"][BITTER]}
        g.dealer = d["dealer"]; g.phase = d["phase"]
        g.offer = Offer(**d["offer"]) if d["offer"] else None
        g.stake = d["stake"]; g.forced = d["forced"]; g.lied = d["lied"]
        g.timeouts = list(d["timeouts"]); g.exchange_no = d["exchange_no"]
        g.decision_no = d["decision_no"] + 1       # старые таймеры не должны сработать
        g.over = d["over"]; g.winner = d["winner"]
        g.log = list(d["log"]); g.history = [Exchange(**x) for x in d["history"]]
        g._last_variant = dict(d["last_variant"]); g.used = set(d.get("used", []))
        g.started = now - d["age"]; g.t0 = now     # время на ход отсчитывается заново
        g.tape_no = d["tape_no"]; g.tape_date = d["tape_date"]; g._tape_base = d["tape_base"]
        g.op = voice.OpState.from_dict(d.get("op"), 2, f"{g.tape_no}:{g.tape_date}")
        if "op" not in d:                # копия из версии без оператора: счёт курса ведём с текущего рассудка
            voice.on_round_start(g.op, g.charges)
        return g

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
        voice.on_round_start(self.op, self.charges)
        self.items = [self.rng.sample(ITEM_KEYS, n_items) for _ in (0, 1)]
        if n >= CUT_FROM_ROUND:
            for p in (0, 1):                 # №05 выдаётся редко и вместо обычного предмета
                if self.items[p] and self.rng.random() < CUT_CHANCE:
                    self.items[p][self.rng.randrange(len(self.items[p]))] = CUT_KEY
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
        if why != "start":
            self._op(voice.on_reload(self.op, self.exchange_no - 1))

    def _op(self, line: str | None) -> None:
        """Реплика оператора отдельной записью журнала (курсивом, как примечание)."""
        if line:
            self.log.append(note_entry(line))

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

    def _end_round(self, w: int, pending: str | None = None) -> None:
        """Курс закрыт, победил w. pending: реплика оператора про последнюю выдачу, если курс кончился на ней."""
        self.wins[w] += 1
        self.log.append(f"КУРС {self.round_no} закрыт. Победа: {P(w)}.")
        cyc = self.exchange_no - 1
        if self.wins[w] >= WINS_NEEDED:
            self._finish(w)
            # конец матча: примечания курса не будет, места в журнале хватает на все реплики
            self._op(pending)
            self._op(voice.after_round(self.op, cyc, self.round_no, [w]))
            self._op(voice.after_match(self.op, cyc, [w], wins=list(self.wins)))
            return
        decider = self.round_no == 2 and self.wins == [1, 1]
        # на стыке курсов одна запись: реплика оператора вместе с примечанием. Про выдачу, на которой кончился
        # курс, оператор говорит вместо итога курса: итог курса и так записан строкой выше
        said = pending or voice.after_round(self.op, cyc, self.round_no, [w], decider=decider)
        self.log.append(note_entry(said, next_note(self.rng)))
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

    def _resolve(self, take: bool, silent: bool = False) -> None:
        """silent: принимающий промолчал, решение принято по таймеру (для оператора)."""
        o = self.offer
        d, r, s = self.dealer, 1 - self.dealer, o.stake
        r_think = round(self.clock() - self.t0)          # сколько решал принимающий
        head = f"{self.exchange_no + 1}. Названо «{PILL_NAME[o.label]}», была {PILL_NAME[o.pill]}. "
        hit = None
        if s == 0:                           # №05: последствия вырезаны, таблетка вскрыта
            text = self._say("void")
        elif take:
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
        # при вырезанных последствиях ответ принимающего ничего не значит: для бота это forced
        self.history.append(Exchange(d, o.pill, o.label, take, o.auto, o.forced or s == 0))
        self.tray[o.pill] -= 1
        self.exchange_no += 1

        killed = False
        if hit is not None:
            self.charges[hit] = max(0, self.charges[hit] - s)
            killed = self.charges[hit] == 0

        said = voice.after_exchange(self.op, voice.Ex(
            dealer=d, receiver=r, pill=o.pill, label=o.label, take=take, stake=s, auto=o.auto, silent=silent,
            forced=o.forced, void=(s == 0), hit=hit, applied=hit is not None, killed=killed,
            charges=tuple(self.charges), max_charges=(self.max_charges, self.max_charges),
            round_no=self.round_no, exchange_no=self.exchange_no,
            heard=True if self.lied is not None else None,
            d_think=o.think, r_think=r_think, tray=(self.tray[CLEAN], self.tray[BITTER]),
        ))
        if killed:
            self._end_round(1 - hit, pending=said)
            return
        self._op(said)

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
        elif kind == CUT_KEY:
            if self.phase != "decide":
                return False, "▒▒▒▒▒▒ срабатывает, когда таблетку уже протянули."
            if self.offer.stake == 0:
                return False, "Последствия уже вырезаны."
        return True, ""

    def use(self, i: int, kind: str) -> Result:
        ok, err = self.can_use(i, kind)
        if not ok:
            return Result(False, err)
        self.items[i].remove(kind)
        self.used.add(kind)
        # РАЗБОР: предмет даёт игроку новое время. Раньше два предмета съедали
        # 25 из 30 секунд, и на саму выдачу оставалось 5.
        self.decision_no += 1
        if kind == "recorder":
            self.lied = self.offer.pill != self.offer.label
            self.log.append(f"Диктофон: {P(i)}. Запись включена.")
            self._op(voice.on_item(self.op, kind, i, self.exchange_no))
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
        elif kind == CUT_KEY:
            self.offer.stake = 0
            self.log.append(f"{CUT_REAL}: {P(i)}. Последствия этой выдачи вырезаны.")
        self._op(voice.on_item(self.op, kind, i, self.exchange_no))
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
            self._end_round(1 - i, pending=voice.on_event(self.op, "auto_double", i, self.exchange_no - 1))
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
            self._resolve(True, silent=True)

    def surrender(self, i: int) -> None:
        if self.over or i not in (0, 1):
            return
        self.log.append(f"Досрочный выход: {P(i)}. Согласие остаётся в силе.")
        self._finish(1 - i)
        self._op(voice.on_event(self.op, "surrender", i, self.exchange_no - 1))


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
        if g.can_use(me, CUT_KEY)[0] and o.stake >= my:
            return ("use", CUT_KEY)             # №05 спасает, когда выдача могла бы закончить курс
        if o.forced or o.stake == 0:
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
