"""Режим «Третий за столом»: двое участников против одного. Про Telegram здесь ничего не известно.

Места: 0 — Третий (один), 1 и 2 — участники (двое). Таблетки, ставки, предметы и лоток те же,
что в engine.py. Что добавлено:

  Дуэли по очереди. Третий играет две обычные выдачи подряд с одним участником (он выдаёт, потом
  тот выдаёт ему; порядок внутри пары задаёт жребий курса), потом так же со вторым. Рассудок у
  Третьего общий на обе «дуэли».
  Курс. Третий выигрывает курс, когда рассудок обоих участников на нуле. Участники выигрывают,
  когда на нуле Третий. Матч до двух курсов.
  Преимущество Третьего. Рассудка у него больше, и «Привыкание»: первая горькая за курс,
  которая должна его ударить, его не берёт (его пробовали раньше).
  Преимущество участников. Их двое: можно говорить в группе, а ещё «Второй наушник»: пока Третий
  выдаёт одному, второй участник может включить Диктофон для друга.
  Выбывший участник. Когда один из участников выбыл, а второй остаётся, выбывшему в личку приходит
  выбор: «Вы поможете своему другу?» Да: у оставшегося внезапно появляется бафф. Нет: дебафф.
  Молчание: не происходит ничего. Выбор ставит игру на паузу на GHOST_SECONDS секунд.
  Курс 3. Названия предметов нечитаемы: у всех «???», кнопки перемешиваются при каждой перерисовке.
  Что это было, узнаёшь только после применения.

  Разногласие. Со 2 курса, после первой потери рассудка у участника, запись может остановиться и
  предъявить обоим «Пункт 12» (Выход). Каждому отдельно, втайне и одновременно; каждому сказано, что
  это предложено ему одному (это неправда, и поэтому после матча запись выдаёт расшифровку).
  Вышел один: получает предложенное. Вышли оба: дверь заклинило, не получает никто. Вышел кто-то:
  Согласие расторгнуто, и участники сидят «порознь» до конца матча:
    · курс записывается тому, кто закрыл Третьего, второй не засчитывается (счёт у каждого свой);
    · Второй наушник не действует;
    · таблетку, которую протянул Третий, можно пододвинуть бывшему напарнику (он решает, Третий
      в этом обмене не участвует).

Числа настраиваются в ROUNDS3 (комментарий над ней). Всё остальное в одном месте: GHOST_SECONDS,
HIDE_FROM_ROUND, WINS_NEEDED, а для Разногласия: DOOR_FROM_ROUND, DOOR_CHANCE, DOOR_SECONDS.
"""
from __future__ import annotations

import math
import random
import time
import zlib
from dataclasses import asdict, dataclass

import voice
from engine import (
    ANOMALIES,
    ANOMALIES_LOW,
    ARCHIVE,
    ARCHIVE_GROUPS,
    BITTER,
    CLEAN,
    CUT_CHANCE,
    CUT_FROM_ROUND,
    CUT_KEY,
    ITEM_KEYS,
    ITEM_NAMES,
    ITEM_REAL,
    ITEM_REAL_HELP,
    LINES,
    MONTHS,
    NOTE_MARK,
    NOTES,
    PILL_NAME,
    SIGNOFFS,
    TAPE_ENDS,
    TAPE_FULL,
    TAPE_LOW,
    TAPE_SLOTS,
    TAPE_USED,
    Exchange,
    Offer,
    Result,
    _eid,
    _flat,
    bot_choose,
    note_entry,
    note_ids,
    P,
)

SOLO, A, B = 0, 1, 2
ALLIES = (A, B)
SIDE_SOLO, SIDE_TEAM = 0, 1          # индексы счёта и победителя

# курс: (рассудок участника, рассудок Третьего, предметов участнику, предметов Третьему, лоток от, до)
# Рассудок Третьего общий на обе «дуэли», поэтому он заметно больше, чем у одного участника,
# и во 2 курсе ему выдают на один предмет больше (в 3 курсе поровну: иначе при «???» он знает весь набор). Числа подобраны прогоном ИИ против ИИ (4000 партий):
# Третий выигрывает матч в ~52% случаев. ИИ-участники не говорят друг с другом и не пользуются
# «Вторым наушником», живые участники сильнее, так что у людей получается примерно поровну.
# Третьему тяжело: уменьши второе число. Участникам тяжело: увеличь.
ROUNDS3 = {
    1: (2, 5, 0, 0, 4, 5),
    2: (2, 6, 2, 3, 4, 6),
    3: (3, 7, 3, 3, 5, 6),
}
WINS_NEEDED = 2
HIDE_FROM_ROUND = 3       # с этого курса названия предметов не видны
GHOST_SECONDS = 20.0      # сколько у выбывшего времени на ответ

# Разногласие («Пункт 12 · Выход»).
DOOR_FROM_ROUND = 2       # с какого курса запись может остановиться и предъявить Выход
DOOR_CHANCE = 0.75        # шанс на каждую потерю рассудка участником, пока дверь в этом курсе не открывалась
DOOR_SECONDS = 25.0       # время на ответ, молчание = «остаться»
BOT_LEAVE = 0.30          # как часто ИИ-участник выходит (только для /trio_test и прогонов баланса)
BOT_SLIDE = 0.10          # как часто ИИ-участник пододвигает таблетку, если сидит порознь

# ----------------------------------------------------------------------
# Тексты режима. Тон тот же: ровно, без восклицаний, одна странная деталь на строку.
# ----------------------------------------------------------------------

NOTES3 = [
    "Участников должно быть двое. За столом трое. Считайте одного.",
    "Третий не числится в протоколе. Это не значит, что его нет.",
    "Если вас двое, а слышно трёх, прислушайтесь, чей голос повторяется.",
    "Голос напарника не обязан принадлежать напарнику.",
    "Не обсуждайте стратегию вслух. Стены слышат. Стены не возражают.",
    "Если Третий кивнул вашему напарнику, вы этого не видели.",
    "Если напарник вышел, а его голос остался, не отвечайте.",
    "Если стало легче, значит, кто-то отдал своё. Не спрашивайте, кто.",
    "Согласие подписано двумя сторонами. Третья сторона подписала раньше.",
    "Если выбывший окликнул вас по имени, ответить можно один раз.",
    "Третий не моргает. Это замечание, а не предупреждение.",
    "Тот, кто сидит один, не обязательно один.",
    "В протоколе нет слова «ничья». В протоколе есть слово «пока».",
    "Выбывшим просьба оставаться на связи. Связь не гарантируется.",
    "Если счётчик показывает троих, перемотайте плёнку. Если и после этого трое, перемотайте участников.",
    "Диктофон записывает не только выдающего. Запись ваша тоже.",
    "Если напарник согласен с каждым вашим словом, это ещё не значит, что это напарник.",
    "Не оборачивайтесь на третий стул. Он не занят. Он не пуст.",
]

ANOMALIES3 = [
    "За столом три тени. Стульев два. Потом снова три.",
    "Кто-то на записи говорит голосом Третьего. Третий молчит.",
    "Запись показывает стол сверху. Камеры там нет.",
    "В кадре на секунду видны трое. Двое смотрят друг на друга. Третий смотрит в камеру.",
    "Счёт на плёнке не совпадает со счётом в комнате.",
    "Голос напарника доносится из лотка. Напарник рядом.",
    "Стул у стены отодвинут. Никто не вставал.",
    "Лампа над столом мигает в такт чьему-то дыханию.",
    "На записи Третий улыбается. В комнате не видно.",
    "На записи у Третьего другое лицо. Оно похоже.",
]

# Что видно вместо названия предмета в курсе, где каталог повреждён.
HIDDEN_USE = [
    "▒▒▒▒▒▒: {p}. Применено.",
    "▒▒▒▒▒▒: {p}. Название не читается.",
    "▒▒▒▒▒▒: {p}. Запись об этом предмете повреждена.",
]
HIDDEN_ROUND = "Каталог предметов повреждён: названия не читаются. Что у кого, станет ясно после применения."

# Свои варианты строк лога (поверх engine.LINES).
LINES3 = {
    "tol_take": [
        "Приём. Вкус знакомый. Рассудок не тронут: {p} пробовал это раньше. Привыкание израсходовано.",
        "Приём. Горечь не берёт: {p} её уже принимал. Привыкание израсходовано.",
    ],
    "tol_refuse": [
        "Отказ. Таблетку принимает {p}. Вкус знакомый, рассудок не тронут. Привыкание израсходовано.",
        "Отказ. Вскрыто при свидетелях. {p} принимает без последствий: пробовал раньше. Привыкание израсходовано.",
    ],
    "shield": [
        "Удар пришёлся на тень. Рассудок не тронут: {p}.",
        "Что-то встало между. Рассудок не тронут: {p}.",
    ],
}

# Выбывший передаёт кадр (да) или нет. Побочный текст пишется в лог, оба слова подставляются при отрисовке.
# good: бафф или дебафф; name и desc нужны архиву.
GHOST_FX = {
    "heal": {
        "good": True, "name": "Остаток плёнки",
        "desc": "Рассудок +1 тому, кто остался за столом (не выше максимума курса).",
        "log": "{g} отдаёт последний кадр. Рассудок +1: {f}. Кадр ещё тёплый.",
    },
    "shield": {
        "good": True, "name": "Тень за спиной",
        "desc": "Следующий удар по оставшемуся приходится на тень и рассудок не отнимает.",
        "log": "{g} встаёт за спиной напарника. Тень: {f}. Следующий удар придётся на неё.",
    },
    "hear": {
        "good": True, "name": "Голос сквозь стену",
        "desc": "Следующую выдачу оставшемуся участнику слышно насквозь: Диктофон бесплатно.",
        "log": "Сквозь стену слышен голос выбывшего. {f}: следующее слово выдающего слышно насквозь.",
    },
    "gift": {
        "good": True, "name": "Лишнее под столом",
        "desc": "Оставшийся находит под столом предмет, которого у него не было.",
        "log": "Под столом у напарника лежит то, чего там не было. Выбывший молчит. Предмет получен: {f}.",
    },
    "cold": {
        "good": False, "name": "Холод",
        "desc": "Рассудок −1 тому, кто остался (не ниже единицы).",
        "log": "{g} не отвечает. В комнате холодает. Рассудок −1: {f}.",
    },
    "rush": {
        "good": False, "name": "Часы идут быстрее",
        "desc": "Время на два следующих хода оставшегося сокращается вдвое.",
        "log": "{g} не отвечает. Часы на стене пошли быстрее. Времени вдвое меньше: {f}.",
    },
    "scramble": {
        "good": False, "name": "Рябь на плёнке",
        "desc": "Предметы оставшегося на два хода превращаются в «???».",
        "log": "{g} не отвечает. Плёнка пошла рябью. Предметы не разобрать: {f}.",
    },
}
GHOST_SILENCE = [
    "{g} молчит. Молчание фиксируется так же, как слова. Ничего не передано.",
    "{g} не ответил. Кадр остался при нём. Это тоже запись.",
]

# ----------------------------------------------------------------------
# Разногласие. Тон тот же. Про условия выхода запись говорит правду. Неправда в одном: будто пункт
# предъявлен только тебе. Он предъявлен обоим. Условия при этом сами проговаривают «если откроют двое»,
# так что внимательный участник может поймать запись на слове.
# ----------------------------------------------------------------------

# kind: (что предлагают, коротко для расшифровки)
DOOR_OFFER = {
    "heal": ("Тебе вернут то, что у тебя забрали: рассудок до максимума.", "рассудок до максимума"),
    "reserve": ("Тебе добавят запас: максимум рассудка +1, и сразу полный.", "максимум рассудка +1"),
    "item": ("Тебе положат под стол предмет, которого у тебя нет. Какой, не указано.", "предмет"),
}
DOOR_LIES = [
    "Этот пункт предъявляется тебе одному. Напарнику он не предъявлялся.",
    "Напарник этого не слышит. Пункт сказан только тебе.",
    "Остальным за столом пункт не зачитывался. Напарнику тоже.",
]
DOOR_TERMS = (
    "Выйти можно один раз. Вышедший перестаёт быть напарником: курс записывается тому, кто закроет Третьего, "
    "второй не засчитывается, Второй наушник не действует. Дверь рассчитана на одного. Если откроют двое, "
    "её заклинит, и предложенное не получит никто."
)

LINES3.update({
    "door_open": [
        "Запись остановилась на полуслове. Читается пункт 12. Кому, не сообщается.",
        "Плёнка встала. Пункт 12 предъявлен. Кому именно, не сообщается.",
        "Запись остановлена. Пункт 12. Слышно, как переворачивают страницу, которой нет.",
        "Лампа погасла и загорелась. Пункт 12 предъявлен. Кому, не сообщается.",
    ],
    "door_timeout": [
        "Время вышло. Молчание на пункт 12 считается отказом.",
        "Время вышло. Кто не ответил, тот остался. Так записано.",
    ],
    "door_shut": [
        "Дверь не открылась. Согласие выдержало. Кто-то за ней ещё стоит.",
        "Дверь осталась закрытой. Согласие выдержало. Запись продолжается.",
        "Никто не вышел. Согласие выдержало. Пока.",
    ],
    "door_one": [
        "Дверь открылась. Вышел один. Согласие расторгнуто.",
        "Дверь открылась на одного. Второй остался за столом. Согласие расторгнуто.",
        "Один стул остыл. Согласие расторгнуто. Кто вышел, видно по лицам.",
    ],
    "door_jam": [
        "Дверь заклинило: вышли оба. Предложенное не выдано. Согласие расторгнуто.",
        "Вышли оба. Дверь рассчитана на одного, и она это помнит. Предложенное не выдано. Согласие расторгнуто.",
        "Дверь вышла из пазов: вышли двое. Предложенное не получил никто. Согласие расторгнуто.",
    ],
    "door_abort": [
        "Пункт 12 снят: один из участников вышел из матча. Запись продолжается.",
    ],
    "slide": [
        "{x} не берёт и не отказывается. Таблетка пододвинута: {y}. Названо «{lab}».",
        "{x} кладёт таблетку на середину стола и толкает к {y}. Названо «{lab}».",
        "Таблетка меняет руки: {x} → {y}. Названо «{lab}». Третий не вмешивается.",
    ],
    "taken": [
        "Третьего закрывает {t}. Курс записан за одним. {o}: не засчитано.",
        "Последний кадр: {t}. Курс записан за одним. {o}: присутствие, не более.",
        "{t} закрывает курс. Записан один. {o}: без записи.",
    ],
})

# Примечания и помехи для тех, кто сидит порознь.
NOTES_RIFT = [
    "Если вам сказали, что вы единственный, пересчитайте стулья.",
    "Согласие расторгнуто. Протокол этого не заметил.",
    "Бывшие напарники записываются как напарники. Протокол консервативен.",
    "Дверь рассчитана на одного. Никто не уточнял, на кого.",
    "Если стул напротив пуст, это не значит, что на нём никого нет.",
    "Таблетку можно пододвинуть. Чья рука, не фиксируется.",
    "Тот, кто вышел, не обязательно вышел.",
    "Если бывший напарник зовёт вас по имени, ответ записывается как согласие.",
    "Закрывший Третьего записывается один. Остальные присутствовали.",
    "Не пододвигайте таблетку тому, кто смотрит на неё дольше, чем на вас.",
    "Тот, кто остался, тоже что-то выбрал. Запись помнит, что.",
    "Дверь была приоткрыта. Это не значит, что в неё вышли. Это значит, что в неё смотрели.",
]
ANOMALIES_RIFT = [
    "Стулья стоят дальше друг от друга, чем в начале записи.",
    "На плёнке двое сидят спиной друг к другу. В комнате лицом.",
    "Дверь в углу кадра приоткрыта. Из неё не дует.",
    "На столе на записи два лотка. Второй пуст и тёплый.",
    "Кто-то на записи окликает участника по имени. Оборачивается другой.",
    "Тень одного из участников лежит на столе отдельно от него.",
    "Над каждым из двоих лампа горит по-своему.",
    "У одного участника на записи нет тени. Потом есть. Потом чужая.",
]

# Записи архива: новая группа «Последние кадры» и пара новых записей в старых.
ARCHIVE_GROUPS.setdefault("last", "Последние кадры")
for _t in NOTES3 + [HIDDEN_ROUND]:
    ARCHIVE[_eid("notes", _t)] = ("notes", _t)
for _t in ANOMALIES3:
    ARCHIVE[_eid("glitches", _t)] = ("glitches", _t)
_LAST = {
    **{f"fx:{k}": ("last", f"«{v['name']}» ({'передал' if v['good'] else 'не передал'}). {v['desc']}")
       for k, v in GHOST_FX.items()},
    "fx:tol": ("last", "«Привыкание». Третьего первая горькая за курс не берёт: он пробовал её раньше."),
    "fx:ear": ("last", "«Второй наушник». Пока Третий выдаёт одному участнику, второй включает Диктофон для друга."),
    "fx:silence": ("last", "«Молчание». Выбывший не ответил. Ничего не передано. Это тоже фиксируется."),
}
ARCHIVE.update(_LAST)

# Группа «Разногласие» и записи в старых группах.
ARCHIVE_GROUPS.setdefault("rift", "Разногласие")
for _t in NOTES_RIFT:
    ARCHIVE[_eid("notes", _t)] = ("notes", _t)
for _t in ANOMALIES_RIFT:
    ARCHIVE[_eid("glitches", _t)] = ("glitches", _t)
ARCHIVE.update({
    "fx:door": ("rift", "«Пункт 12 · Выход». Со 2 курса запись может остановиться и предъявить участникам выход: "
                        "каждому отдельно, втайне, под него. Каждому говорят, что это ему одному."),
    "fx:shut": ("rift", "«Дверь не открылась». Оба остались. Согласие выдержало. Пока."),
    "fx:open": ("rift", "«Дверь открылась». Вышел один и получил предложенное. Второй остался за столом, но уже порознь."),
    "fx:jam": ("rift", "«Дверь заклинило». Вышли оба. Дверь рассчитана на одного: предложенное не получил никто."),
    "fx:split": ("rift", "«Порознь». Согласие расторгнуто до конца матча: курс записывается тому, кто закрыл Третьего, "
                         "Второй наушник не действует."),
    "fx:slide": ("rift", "«Пододвинуть». Порознь таблетку, протянутую Третьим, можно передать бывшему напарнику, "
                         "назвав её по-своему. Решает он."),
    "fx:taken": ("rift", "«Закрывший». Порознь курс записывается одному: тому, кто закрыл Третьего. Второй присутствовал."),
    "fx:lie": ("rift", "«Расшифровка пункта 12». После матча запись показывает, что сказали каждому. "
                       "Каждому сказали, что он один."),
})

_NOTE_BAG3: list[str] = []
_NOTE_LAST3: str | None = None
_NOTE_BAG_RIFT: list[str] = []


def next_note3(rng: random.Random, split: bool = False) -> str:
    """Примечания режима вперемешку с обычными: круг на весь процесс, подряд одно и то же не выпадает.
    Если участники сидят порознь, примерно каждое второе примечание про это."""
    global _NOTE_LAST3
    if split and rng.random() < 0.5:
        if not _NOTE_BAG_RIFT:
            bag = NOTES_RIFT[:]
            rng.shuffle(bag)
            _NOTE_BAG_RIFT.extend(bag)
        _NOTE_LAST3 = _NOTE_BAG_RIFT.pop()
        return _NOTE_LAST3
    if not _NOTE_BAG3:
        bag = NOTES3 + NOTES3 + NOTES[:8]
        bag = list(dict.fromkeys(bag))
        rng.shuffle(bag)
        if _NOTE_LAST3 is not None and bag[-1] == _NOTE_LAST3:
            bag[0], bag[-1] = bag[-1], bag[0]
        _NOTE_BAG3.extend(bag)
    _NOTE_LAST3 = _NOTE_BAG3.pop()
    return _NOTE_LAST3


@dataclass
class Exchange3:
    dealer: int
    receiver: int
    pill: str
    label: str
    take: bool
    auto: bool
    forced: bool
    passed: bool = False             # таблетку пододвинули (Третий в этом обмене не участвовал)


class Trio:
    def __init__(self, names, rng: random.Random | None = None, clock=time.monotonic,
                 turn_seconds: float = 30.0, solo_first: bool | None = None):
        if len(names) != 3:
            raise ValueError("нужно три имени: Третий и двое участников")
        self.rng = rng or random.Random()
        self.clock = clock
        self.turn_seconds = float(turn_seconds)
        self.names = list(names)

        self.pts = [0, 0, 0]                     # курсы по местам: [Третий, участник 1, участник 2]
        self.split = False                       # Согласие расторгнуто: участники сидят порознь
        self.leavers: set[int] = set()           # кто выходил в Пункте 12
        self.winners: list[int] = []             # места победителей, когда матч окончен
        self.round_no = 0
        self.charges = [0, 0, 0]
        self.max_charges = [0, 0, 0]
        self.items: list[list[str]] = [[], [], []]
        self.tray = {CLEAN: 0, BITTER: 0}

        self.phase = "deal"                      # deal | decide | ghost | door | over
        self.dealer, self.receiver = SOLO, A
        self.offer: Offer | None = None
        self.stake = 1
        self.forced = False
        self.lied_for: dict[int, bool] = {}      # кто что услышал на Диктофоне в этой выдаче

        self.tolerance_used = False              # «Привыкание» Третьего в этом курсе
        self.shield = [0, 0, 0]
        self.hear = [False, False, False]
        self.rush = [0, 0, 0]
        self.scramble = [0, 0, 0]
        self.ghost_seat: int | None = None
        self.ghost_for: int | None = None
        self._after_ghost = "advance"            # advance: обмен состоялся; restart: обмен отменён

        self.door_ans: list = [None, None, None]     # ответ на Пункт 12: None | True (выйти) | False
        self.door_silent = [False, False, False]     # ответ дан молчанием (вышло время)
        self.door_kind: list = [None, None, None]    # что предложено каждому: heal | reserve | item
        self.door_lie = [0, 0, 0]                    # какая из DOOR_LIES сказана каждому
        self.door_round = 0                          # в каком курсе дверь уже открывалась
        self.door_log: list[dict] = []               # для расшифровки после матча
        self.passed = False                          # текущая выдача: таблетку пододвинули
        self.slide_first = CLEAN                     # что назвал Третий, пока таблетку не пододвинули

        self.cur_ally = A
        self.sub = 0                                 # 0: первая выдача пары, 1: вторая
        self.solo_first = True
        self._ally_flip = self.rng.randint(0, 1)
        self.out: set[int] = set()               # участники, вышедшие из матча совсем

        self.timeouts = [0, 0, 0]
        self.exchange_no = 0
        self.decision_no = 0
        self.over = False
        self.winner: int | None = None           # SIDE_SOLO | SIDE_TEAM
        self.log: list[str] = []
        self.history: list[Exchange3] = []
        self._last_variant: dict[str, str] = {}
        self.used_pub: set[str] = set()
        self.used_by: list[set[str]] = [set(), set(), set()]
        self.seen_fx: set[str] = set()

        self.started = self.t0 = self.clock()
        self.tape_no = f"{self.rng.randint(1, 999):04d}"
        self.tape_date = f"{self.rng.randint(1, 28)} {self.rng.choice(MONTHS)} 98"
        self._tape_base = self.rng.randint(21 * 3600, 23 * 3600 + 3000)
        self.op = voice.OpState(3, f"{self.tape_no}:{self.tape_date}")     # память оператора (voice.py)

        self.log.append("Согласие подписано обеими сторонами. Третья сторона подписала раньше.")
        first = solo_first if solo_first is not None else self.rng.random() < 0.5
        self._start_round(1, first)

    # ------------------------------------------------------------------
    # Состояние для отрисовки
    # ------------------------------------------------------------------

    @property
    def turn(self) -> int:
        """Чьего действия ждём. -1, когда партия окончена."""
        if self.phase == "deal":
            return self.dealer
        if self.phase == "decide":
            return self.receiver
        if self.phase == "ghost":
            return self.ghost_seat
        if self.phase == "door":
            pend = self.door_pending()
            return pend[0] if pend else ALLIES[0]
        return -1

    @property
    def wins(self) -> list[int]:
        """[Третий, лучший из участников]: для мест, где счёт показывался по сторонам."""
        return [self.pts[SOLO], max(self.pts[A], self.pts[B])]

    def is_winner(self, i: int) -> bool:
        return i in self.winners

    def alive(self, i: int) -> bool:
        return self.charges[i] > 0

    def door_pending(self) -> list[int]:
        """Участники, которые ещё не ответили на Пункт 12."""
        if self.phase != "door":
            return []
        return [a for a in ALLIES if self.alive(a) and self.door_ans[a] is None]

    def allies_alive(self) -> list[int]:
        return [a for a in ALLIES if self.alive(a)]

    def pills(self) -> list[str]:
        return [k for k in (CLEAN, BITTER) if self.tray[k] > 0]

    def count(self, p: int, kind: str) -> int:
        return self.items[p].count(kind)

    def tray_total(self) -> int:
        return self.tray[CLEAN] + self.tray[BITTER]

    def hidden_public(self) -> bool:
        """Каталог предметов повреждён для всех: ни названий, ни числа."""
        return self.round_no >= HIDE_FROM_ROUND

    def hidden_for(self, i: int) -> bool:
        """Видит ли игрок i названия своих предметов."""
        return self.hidden_public() or (i in (0, 1, 2) and self.scramble[i] > 0)

    def observer(self) -> int | None:
        """Участник, который может включить Диктофон для друга (Третий выдаёт другому)."""
        if self.phase != "decide" or self.receiver not in ALLIES:
            return None
        if self.split or self.dealer != SOLO:        # порознь наушника нет
            return None
        other = B if self.receiver == A else A
        return other if self.alive(other) else None

    def time_limit(self, i: int) -> float:
        if self.phase == "ghost":
            return GHOST_SECONDS
        if self.phase == "door":
            return DOOR_SECONDS
        return self.turn_seconds / 2 if self.rush[i] > 0 else self.turn_seconds

    def role(self, i: int) -> str:
        return "Третий" if i == SOLO else "Участник"

    def tape_clock(self) -> str:
        t = (self._tape_base + int(self.clock() - self.started)) % 86400
        return f"{t // 3600:02d}:{t % 3600 // 60:02d}"

    def tape_left(self) -> int:
        if self.over:
            return 0
        return max(1, TAPE_SLOTS - self.exchange_no // 4)

    def tape_bar(self) -> str:
        left = self.tape_left()
        return TAPE_FULL * left + TAPE_USED * (TAPE_SLOTS - left)

    def tape_end(self) -> str:
        rnd = random.Random(zlib.crc32(f"{self.tape_no}:tapeend3".encode()))
        return rnd.choice(TAPE_ENDS)

    def signoff(self) -> str:
        rnd = random.Random(zlib.crc32(f"{self.tape_no}:end3".encode()))
        return rnd.choice(SIGNOFFS)

    def anomaly(self, viewer: int | None) -> str | None:
        """Помеха плёнки для кадра или None. Зависит только от состояния: перерисовка её не меняет."""
        if self.over:
            return None
        if self.tape_left() <= 1:
            return TAPE_LOW[self.exchange_no % len(TAPE_LOW)]
        low = (viewer in (0, 1, 2) and self.max_charges[viewer] >= 2 and self.charges[viewer] == 1)
        seed = f"{self.tape_no}:{self.exchange_no}:{self.phase}:{int(low)}:{int(self.split)}:3"
        rnd = random.Random(zlib.crc32(seed.encode()))
        chance = {1: 0.07, 2: 0.13, 3: 0.20}.get(self.round_no, 0.2) + (0.12 if low else 0.0)
        if self.split:
            chance += 0.08
        if rnd.random() >= chance:
            return None
        if low and rnd.random() < 0.6:
            return rnd.choice(ANOMALIES_LOW)
        if self.split and rnd.random() < 0.5:
            return rnd.choice(ANOMALIES_RIFT)
        return rnd.choice(ANOMALIES + ANOMALIES3)

    def finds(self, viewer: int) -> list[str]:
        """id записей архива, которые игрок видит сейчас или которые уже случились в партии."""
        out: list[str] = []
        for line in self.log[-8:]:
            if line.startswith(NOTE_MARK):
                out += note_ids(line)
        a = self.anomaly(viewer)
        if a:
            out.append(_eid("glitches", a))
        if self.over:
            out.append(_eid("ends", _flat(self.tape_end())))
            out.append(_eid("ends", self.signoff()))
        if self.over and self.door_log:
            out.append("fx:lie")
        out += [f"i:{k}" for k in sorted(self.used_by[viewer] | self.used_pub)]
        out += [f"fx:{k}" for k in sorted(self.seen_fx)]
        return [e for e in out if e in ARCHIVE]

    # ------------------------------------------------------------------
    # Сохранение и восстановление
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        ver, internal, gauss = self.rng.getstate()
        return {
            "rng": [ver, list(internal), gauss], "turn_seconds": self.turn_seconds,
            "names": list(self.names), "pts": list(self.pts), "round_no": self.round_no,
            "split": self.split, "leavers": sorted(self.leavers), "winners": list(self.winners),
            "door_ans": list(self.door_ans), "door_silent": list(self.door_silent),
            "door_kind": list(self.door_kind), "door_lie": list(self.door_lie),
            "door_round": self.door_round, "door_log": [dict(x) for x in self.door_log],
            "passed": self.passed, "slide_first": self.slide_first,
            "charges": list(self.charges), "max_charges": list(self.max_charges),
            "items": [list(x) for x in self.items], "tray": dict(self.tray),
            "phase": self.phase, "dealer": self.dealer, "receiver": self.receiver,
            "offer": asdict(self.offer) if self.offer else None,
            "stake": self.stake, "forced": self.forced,
            "lied_for": {str(k): v for k, v in self.lied_for.items()},
            "tolerance_used": self.tolerance_used, "shield": list(self.shield),
            "hear": list(self.hear), "rush": list(self.rush), "scramble": list(self.scramble),
            "ghost_seat": self.ghost_seat, "ghost_for": self.ghost_for, "after_ghost": self._after_ghost,
            "cur_ally": self.cur_ally, "sub": self.sub, "solo_first": self.solo_first,
            "ally_flip": self._ally_flip, "out": sorted(self.out),
            "timeouts": list(self.timeouts), "exchange_no": self.exchange_no,
            "decision_no": self.decision_no, "over": self.over, "winner": self.winner,
            "log": list(self.log), "history": [asdict(x) for x in self.history],
            "last_variant": dict(self._last_variant), "used_pub": sorted(self.used_pub),
            "used_by": [sorted(s) for s in self.used_by], "seen_fx": sorted(self.seen_fx),
            "age": self.clock() - self.started,
            "tape_no": self.tape_no, "tape_date": self.tape_date, "tape_base": self._tape_base,
            "op": self.op.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict, clock=time.monotonic) -> "Trio":
        g = cls.__new__(cls)
        now = clock()
        g.clock = clock
        g.rng = random.Random()
        ver, internal, gauss = d["rng"]
        g.rng.setstate((ver, tuple(internal), gauss))
        g.turn_seconds = float(d.get("turn_seconds", 30.0))
        g.names = list(d["names"]); g.round_no = d["round_no"]
        if "pts" in d:
            g.pts = list(d["pts"])
        else:                                    # копия из версии, где счёт был по сторонам
            g.pts = [d["wins"][0], d["wins"][1], d["wins"][1]]
        g.split = bool(d.get("split", False)); g.leavers = set(d.get("leavers", []))
        g.winners = list(d.get("winners", []))
        if d.get("over") and not g.winners:         # копия окончена в старой версии
            g.winners = [0] if d.get("winner") == 0 else [a for a in ALLIES if a not in set(d["out"])]
        g.door_ans = list(d.get("door_ans", [None, None, None]))
        g.door_silent = list(d.get("door_silent", [False, False, False]))
        g.door_kind = list(d.get("door_kind", [None, None, None]))
        g.door_lie = list(d.get("door_lie", [0, 0, 0]))
        g.door_round = d.get("door_round", 0)
        g.door_log = [dict(x) for x in d.get("door_log", [])]
        g.passed = bool(d.get("passed", False)); g.slide_first = d.get("slide_first", CLEAN)
        g.charges = list(d["charges"]); g.max_charges = list(d["max_charges"])
        g.items = [list(x) for x in d["items"]]
        g.tray = {CLEAN: d["tray"][CLEAN], BITTER: d["tray"][BITTER]}
        g.phase = d["phase"]; g.dealer = d["dealer"]; g.receiver = d["receiver"]
        g.offer = Offer(**d["offer"]) if d["offer"] else None
        g.stake = d["stake"]; g.forced = d["forced"]
        g.lied_for = {int(k): bool(v) for k, v in d["lied_for"].items()}
        g.tolerance_used = d["tolerance_used"]; g.shield = list(d["shield"])
        g.hear = list(d["hear"]); g.rush = list(d["rush"]); g.scramble = list(d["scramble"])
        g.ghost_seat = d["ghost_seat"]; g.ghost_for = d["ghost_for"]; g._after_ghost = d["after_ghost"]
        g.cur_ally = d["cur_ally"]; g.sub = d["sub"]; g.solo_first = d["solo_first"]
        g._ally_flip = d["ally_flip"]; g.out = set(d["out"])
        g.timeouts = list(d["timeouts"]); g.exchange_no = d["exchange_no"]
        g.decision_no = d["decision_no"] + 1
        g.over = d["over"]; g.winner = d["winner"]
        g.log = list(d["log"]); g.history = [Exchange3(**x) for x in d["history"]]
        g._last_variant = dict(d["last_variant"]); g.used_pub = set(d["used_pub"])
        g.used_by = [set(s) for s in d["used_by"]]; g.seen_fx = set(d["seen_fx"])
        g.started = now - d["age"]; g.t0 = now
        g.tape_no = d["tape_no"]; g.tape_date = d["tape_date"]; g._tape_base = d["tape_base"]
        g.op = voice.OpState.from_dict(d.get("op"), 3, f"{g.tape_no}:{g.tape_date}")
        if "op" not in d:                # копия из версии без оператора: счёт курса ведём с текущего рассудка
            voice.on_round_start(g.op, g.charges)
        return g

    # ------------------------------------------------------------------
    # Курсы, лоток, порядок выдач
    # ------------------------------------------------------------------

    def _say(self, key: str, **kw) -> str:
        options = LINES3.get(key) or LINES[key]
        last = self._last_variant.get(key)
        pool = [o for o in options if o != last] or options
        choice = self.rng.choice(pool)
        self._last_variant[key] = choice
        return choice.format(**kw)

    def _start_round(self, n: int, solo_first: bool) -> None:
        ally_c, solo_c, ally_it, solo_it, _, _ = ROUNDS3[n]
        self.round_no = n
        self.charges = [solo_c, ally_c, ally_c]
        for a in self.out:
            self.charges[a] = 0
        voice.on_round_start(self.op, self.charges)
        self.max_charges = list(self.charges)
        self.items = [[], [], []]
        for p in (0, 1, 2):
            k = solo_it if p == SOLO else ally_it
            if k and self.alive(p):
                self.items[p] = self.rng.sample(ITEM_KEYS, k)
                if n >= CUT_FROM_ROUND and self.rng.random() < CUT_CHANCE:
                    self.items[p][self.rng.randrange(len(self.items[p]))] = CUT_KEY
        self.tolerance_used = False
        self.shield = [0, 0, 0]
        self.hear = [False, False, False]
        self.rush = [0, 0, 0]
        self.scramble = [0, 0, 0]
        self.ghost_seat = self.ghost_for = None
        self.timeouts = [0, 0, 0]
        self.solo_first = solo_first
        alive = self.allies_alive()
        self._ally_flip ^= 1
        self.cur_ally = alive[0] if len(alive) == 1 else ALLIES[self._ally_flip]
        self.sub = 0

        tail = {1: "", 2: "", 3: " Следующего курса нет."}[n]
        self.log.append(
            f"КУРС {n}. Рассудок: {P(SOLO)} {solo_c}, участники по {ally_c}. "
            + ("Предметы выданы." if ally_it else "Предметы не выдаются.")
            + tail
        )
        if n >= HIDE_FROM_ROUND:
            self.log.append(NOTE_MARK + HIDDEN_ROUND)
        self._load_tray("start")
        self._begin_deal()
        self.log.append(f"Первая выдача: {P(self.dealer)} → {P(self.receiver)}.")

    def _load_tray(self, why: str, leftover: tuple[str, int] | None = None) -> None:
        lo, hi = ROUNDS3[self.round_no][4:6]
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
        c, b = self.tray[CLEAN], self.tray[BITTER]
        if c and b:
            return
        if c + b == 0:
            self._load_tray("empty")
        else:
            self._load_tray("mono", (CLEAN if c else BITTER, c + b))

    def _pair_roles(self) -> tuple[int, int]:
        solo_deals = (self.sub == 0) == self.solo_first
        return (SOLO, self.cur_ally) if solo_deals else (self.cur_ally, SOLO)

    def _advance(self) -> None:
        """Следующая выдача: вторая в паре, потом пара с другим участником."""
        alive = self.allies_alive()
        if self.cur_ally in alive and self.sub == 0:
            self.sub = 1
            return
        self.sub = 0
        others = [a for a in alive if a != self.cur_ally]
        self.cur_ally = others[0] if others else alive[0]

    def _begin_deal(self) -> None:
        self.phase = "deal"
        self.offer = None
        self.stake = 1
        self.forced = False
        self.passed = False
        self.lied_for = {}
        self.dealer, self.receiver = self._pair_roles()
        self.t0 = self.clock()
        self.decision_no += 1

    def _continue(self, by_hit: bool) -> None:
        """Дальше после обмена (by_hit) или после отменённого обмена."""
        if by_hit:
            self._advance()
            self._maybe_reload()
        else:
            alive = self.allies_alive()
            if self.cur_ally not in alive:
                self.cur_ally, self.sub = alive[0], 0
        self._begin_deal()

    def _end_round(self, side: int, taker: int | None = None, pending: str | None = None) -> None:
        """Курс закрыт. side: кто выиграл. taker: если участники сидят порознь, тот, кто закрыл Третьего.
        pending: реплика оператора про последнюю выдачу, если курс кончился на ней."""
        self.phase = "over"            # чтобы между строками никто не успел сходить
        closers: list[int]
        if side == SIDE_SOLO:
            self.pts[SOLO] += 1
            closers = [SOLO]
            self.log.append(f"КУРС {self.round_no} закрыт. Победа: {P(SOLO)}.")
        elif self.split:
            if taker not in ALLIES:
                alive = self.allies_alive()
                taker = self.cur_ally if self.cur_ally in alive or not alive else alive[0]
            other = B if taker == A else A
            self.pts[taker] += 1
            closers = [taker]
            self.seen_fx.add("taken")
            self.log.append(f"КУРС {self.round_no} закрыт. " + self._say("taken", t=P(taker), o=P(other)))
        else:
            self.pts[A] += 1
            self.pts[B] += 1
            closers = [A, B]
            self.log.append(f"КУРС {self.round_no} закрыт. Победа участников: {P(A)}, {P(B)}.")
        self.door_ans = [None, None, None]
        cyc = self.exchange_no - 1
        special = "t_solo_round" if side == SIDE_SOLO else ("t_taken" if self.split else "t_team_round")
        if (max(self.pts) >= WINS_NEEDED or self.round_no >= len(ROUNDS3)
                or all(a in self.out for a in ALLIES)):
            self._finish_by_points(closers)
            # конец матча: примечания курса не будет, места в журнале хватает на все реплики
            self._op(pending)
            self._op(voice.after_round(self.op, cyc, self.round_no, closers, special=special))
            self._op(voice.after_match(self.op, cyc, self.winners, trio=True))
            return
        # на стыке курсов одна запись: реплика оператора вместе с примечанием (см. engine.note_entry)
        said = pending or voice.after_round(self.op, cyc, self.round_no, closers, special=special)
        self.log.append(note_entry(said, next_note3(self.rng, self.split)))
        self._start_round(self.round_no + 1, side == SIDE_TEAM)    # проигравший курс выдаёт первым

    def _finish_by_points(self, closers: list[int]) -> None:
        """Матч окончен: побеждает набравший два курса. Если после последнего курса счёт не решил, побеждает
        тот, кто закрыл последний курс («закрывший запись»)."""
        if all(a in self.out for a in ALLIES):
            self._finish(SIDE_SOLO, [SOLO])
            return
        top = max(self.pts[x] for x in (SOLO, A, B) if x not in self.out)
        leaders = [x for x in (SOLO, A, B) if self.pts[x] == top and x not in self.out]
        if len(leaders) > 1:
            leaders = [x for x in leaders if x in closers] or leaders
        self._finish(SIDE_SOLO if SOLO in leaders else SIDE_TEAM, leaders)

    def _finish(self, side: int, winners: list[int] | None = None) -> None:
        self.over = True
        self.winner = side
        if winners is None:
            winners = [SOLO] if side == SIDE_SOLO else [a for a in ALLIES if a not in self.out]
        self.winners = list(winners)
        self.phase = "over"
        self.offer = None
        self.door_ans = [None, None, None]
        self.ghost_seat = self.ghost_for = None

    # ------------------------------------------------------------------
    # Падение участника и выбор выбывшего
    # ------------------------------------------------------------------

    def _ally_falls(self, i: int, *, by_hit: bool, ghost_ok: bool, pending: str | None = None) -> None:
        """Участник i на нуле. by_hit: обмен состоялся (таблетка списана); иначе обмен отменён.
        pending: реплика оператора про последнюю выдачу или тайм-аут."""
        self.charges[i] = 0
        self.items[i] = []
        alive = self.allies_alive()
        if not alive:
            self._end_round(SIDE_SOLO, pending=pending)
            return
        self._op(pending)
        if ghost_ok:
            self.phase = "ghost"
            self.ghost_seat, self.ghost_for = i, alive[0]
            self._after_ghost = "advance" if by_hit else "restart"
            self.offer = None
            self.t0 = self.clock()
            self.decision_no += 1
            if self.split:
                self.log.append(
                    f"{P(i)} выбыл: рассудок на нуле. Они больше не напарники, но запись о нём ещё не стёрта: "
                    f"он решает, передать ли что-то тому, кто остался ({P(alive[0])})."
                )
            else:
                self.log.append(
                    f"{P(i)} выбыл: рассудок на нуле. Запись о нём ещё не стёрта: "
                    f"он решает, передать ли что-то оставшемуся напарнику ({P(alive[0])})."
                )
            return
        self._continue(by_hit)

    def _fx_pool(self, f: int, good: bool) -> list[str]:
        pool: list[str] = []
        if good:
            if self.charges[f] < self.max_charges[f]:
                pool.append("heal")
            if self.shield[f] == 0:
                pool.append("shield")
            if not self.hear[f]:
                pool.append("hear")
            if self.round_no >= 2 and self._gift_kinds(f):
                pool.append("gift")
            return pool or ["shield"]
        if self.charges[f] >= 2:
            pool.append("cold")
        if self.rush[f] == 0:
            pool.append("rush")
        if self.round_no == 2 and self.items[f] and self.scramble[f] == 0:
            pool.append("scramble")
        return pool or ["rush"]

    def _gift_kinds(self, f: int) -> list[str]:
        return [k for k in ITEM_KEYS if k not in self.items[f]]

    def _apply_fx(self, key: str, f: int, g: int) -> None:
        if key == "heal":
            self.charges[f] += 1
        elif key == "shield":
            self.shield[f] = 1
        elif key == "hear":
            self.hear[f] = True
        elif key == "gift":
            self.items[f].append(self.rng.choice(self._gift_kinds(f)))
        elif key == "cold":
            self.charges[f] -= 1
        elif key == "rush":
            self.rush[f] = 2
        elif key == "scramble":
            self.scramble[f] = 2
        self.seen_fx.add(key)
        self.log.append(GHOST_FX[key]["log"].format(g=P(g), f=P(f)))

    def ghost_choose(self, i: int, yes: bool) -> Result:
        if self.over or self.phase != "ghost" or i != self.ghost_seat:
            return Result(False, "Сейчас не твой выбор.")
        f = self.ghost_for
        key = self.rng.choice(self._fx_pool(f, yes))
        self._apply_fx(key, f, i)
        self._op(voice.on_event(self.op, "t_ghost_yes" if yes else "t_ghost_no", i, self.exchange_no - 1))
        self._resume_after_ghost()
        return Result(True, private="Передано." if yes else "Оставлено.")

    def _ghost_silence(self) -> None:
        g = self.ghost_seat
        self.seen_fx.add("silence")
        self.log.append(self._say_ghost_silence(g))
        self._op(voice.on_event(self.op, "t_ghost_silence", g, self.exchange_no - 1))
        self._resume_after_ghost()

    def _say_ghost_silence(self, g: int) -> str:
        last = self._last_variant.get("silence")
        pool = [o for o in GHOST_SILENCE if o != last] or GHOST_SILENCE
        choice = self.rng.choice(pool)
        self._last_variant["silence"] = choice
        return choice.format(g=P(g))

    def _resume_after_ghost(self) -> None:
        by_hit = self._after_ghost == "advance"
        self.ghost_seat = self.ghost_for = None
        self._continue(by_hit)

    # ------------------------------------------------------------------
    # Разногласие: Пункт 12 («Выход»)
    # ------------------------------------------------------------------

    def _door_due(self) -> bool:
        """Запись останавливается после потери рассудка участником: со 2 курса, один раз за курс,
        пока Согласие не расторгнуто и оба участника за столом."""
        if (self.round_no < DOOR_FROM_ROUND or self.split or self.door_round == self.round_no
                or len(self.allies_alive()) < 2):
            return False
        return self.rng.random() < DOOR_CHANCE

    def _door_kind(self, i: int) -> str:
        """Запись предлагает то, чего не хватает: потерявшему рассудок его возвращают, остальным добавляют."""
        if self.charges[i] < self.max_charges[i]:
            return "heal"
        kinds = ["reserve"] + (["item"] if self.round_no >= 2 and self._gift_kinds(i) else [])
        return self.rng.choice(kinds)

    def _open_door(self) -> None:
        self.phase = "door"
        self.offer = None
        self.door_round = self.round_no
        for a in ALLIES:
            self.door_ans[a] = None
            self.door_silent[a] = False
            self.door_kind[a] = self._door_kind(a)
            self.door_lie[a] = self.rng.randrange(len(DOOR_LIES))
        self.t0 = self.clock()
        self.decision_no += 1
        self.seen_fx.add("door")
        self.log.append(self._say("door_open"))

    def door_choose(self, i: int, leave: bool) -> Result:
        if self.over or self.phase != "door" or i not in ALLIES or not self.alive(i):
            return Result(False, "Сейчас не твой выбор.")
        if self.door_ans[i] is not None:
            return Result(False, "Ответ уже принят.")
        self.door_ans[i] = bool(leave)
        if not self.door_pending():
            self._door_resolve()
        return Result(True, private="Принято. Изменить нельзя." if leave else "Принято.")

    def _door_timeout(self) -> None:
        for a in self.door_pending():
            self.door_ans[a] = False
            self.door_silent[a] = True
        self.log.append(self._say("door_timeout"))
        self._door_resolve()

    def _door_gift(self, i: int) -> None:
        kind = self.door_kind[i]
        if kind == "heal":
            self.charges[i] = max(self.charges[i], self.max_charges[i])
        elif kind == "item" and self._gift_kinds(i):
            self.items[i].append(self.rng.choice(self._gift_kinds(i)))
        else:
            self.max_charges[i] += 1
            self.charges[i] += 1

    def _door_resolve(self) -> None:
        self.door_log.append({
            "round": self.round_no, "kind": list(self.door_kind), "lie": list(self.door_lie),
            "ans": [None, bool(self.door_ans[A]), bool(self.door_ans[B])], "silent": list(self.door_silent),
        })
        left = [a for a in ALLIES if self.door_ans[a]]
        cyc = self.exchange_no - 1
        if not left:
            self.seen_fx.add("shut")
            self.log.append(self._say("door_shut"))
            self._op(voice.on_event(self.op, "t_door_shut", None, cyc))
        elif len(left) == 1:
            self._door_gift(left[0])
            self.split = True
            self.leavers.add(left[0])
            self.seen_fx.update(("open", "split"))
            self.log.append(self._say("door_one"))
            self._op(voice.on_event(self.op, "t_door_one", None, cyc))
        else:
            self.split = True
            self.leavers.update(left)
            self.seen_fx.update(("jam", "split"))
            self.log.append(self._say("door_jam"))
            self._op(voice.on_event(self.op, "t_door_jam", None, cyc))
        self.door_ans = [None, None, None]
        self._continue(True)                     # обмен, после которого встала запись, уже состоялся

    def _door_abort(self) -> None:
        """Участник вышел из матча, пока дверь была открыта: пункт снимается."""
        self.log.append(self._say("door_abort"))
        self.door_ans = [None, None, None]
        self._continue(True)

    def transcript(self, names) -> list[str]:
        """Расшифровка Пункта 12 для конца матча: что сказали каждому и что вышло. Пусто, если двери не было."""
        out: list[str] = []
        for rec in self.door_log:
            out.append(f"КУРС {rec['round']} · ПУНКТ 12")
            for a in ALLIES:
                lie = DOOR_LIES[rec["lie"][a]]
                what = DOOR_OFFER[rec["kind"][a]][1]
                if rec["ans"][a]:
                    ans = "выход"
                else:
                    ans = "остаться (молчание)" if rec["silent"][a] else "остаться"
                out.append(f"{names[a]}: «{lie}» Предложено: {what}. Ответ: {ans}.")
            out.append("Предъявлено обоим. Каждому сказали, что ему одному.")
        return out

    # ------------------------------------------------------------------
    # Порознь: пододвинуть таблетку
    # ------------------------------------------------------------------

    def can_slide(self, i: int) -> tuple[bool, str]:
        if self.over or self.phase != "decide" or i != self.receiver or i not in ALLIES:
            return False, "Сейчас не твоё решение."
        if not self.split:
            return False, "Пододвигать можно только порознь."
        if self.passed or self.dealer != SOLO:
            return False, "Таблетку уже пододвигали."
        other = B if i == A else A
        if not self.alive(other):
            return False, "Некому пододвинуть."
        if self.offer.forced:
            return False, "Пункт 7: пододвигать нельзя."
        if self.offer.stake == 0:
            return False, "Последствия вырезаны: пододвигать нечего."
        return True, ""

    def slide(self, i: int, label: str) -> Result:
        """Не принять и не отказаться, а пододвинуть таблетку бывшему напарнику, назвав её по-своему.
        Решает он, Третий в этом обмене не участвует."""
        ok, err = self.can_slide(i)
        if not ok:
            return Result(False, err)
        if label not in (CLEAN, BITTER):
            return Result(False, "Неверный выбор.")
        y = B if i == A else A
        o = self.offer
        self._tick(i)
        self.slide_first = o.label
        self.passed = True
        self.seen_fx.add("slide")
        think = round(self.clock() - self.t0)
        self.offer = Offer(o.pill, label, o.stake, False, think, False)
        self.dealer, self.receiver = i, y
        self.lied_for = {}
        self.log.append(self._say("slide", x=P(i), y=P(y), lab=PILL_NAME[label]))
        self._op(voice.on_event(self.op, "t_slide", None, self.exchange_no - 1))
        if self.hear[y]:
            self.hear[y] = False
            self.lied_for[y] = o.pill != label
            self.log.append(f"Голос сквозь стену: {P(y)} слышит выдачу насквозь.")
        self.t0 = self.clock()
        self.decision_no += 1
        return Result(True)

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------

    def _tick(self, i: int) -> None:
        """Игрок i закончил свой ход: эффекты на «два хода» убывают."""
        self.timeouts[i] = 0
        if self.rush[i] > 0:
            self.rush[i] -= 1
        if self.scramble[i] > 0:
            self.scramble[i] -= 1

    def deal(self, i: int, pill: str, label: str) -> Result:
        if self.over or self.phase != "deal" or i != self.dealer:
            return Result(False, "Сейчас не твоя выдача.")
        if pill not in (CLEAN, BITTER) or label not in (CLEAN, BITTER):
            return Result(False, "Неверный выбор.")
        if self.tray[pill] <= 0:
            return Result(False, "Таких таблеток в лотке нет.")
        self._tick(i)
        self._offer(pill, label)
        return Result(True)

    def _offer(self, pill: str, label: str, auto: bool = False) -> None:
        think = round(self.clock() - self.t0)
        self.offer = Offer(pill, label, self.stake, self.forced, think, auto)
        self.lied_for = {}
        r = self.receiver
        if self.hear[r]:
            self.hear[r] = False
            self.lied_for[r] = pill != label
            self.log.append(f"Голос сквозь стену: {P(r)} слышит выдачу насквозь.")
        self.phase = "decide"
        self.t0 = self.clock()
        self.decision_no += 1

    def answer(self, i: int, take: bool) -> Result:
        if self.over or self.phase != "decide" or i != self.receiver:
            return Result(False, "Сейчас не твоё решение.")
        if not take and self.offer.forced:
            return Result(False, "Пункт 7: отказаться нельзя.")
        self._tick(i)
        self._resolve(take)
        return Result(True)

    def _resolve(self, take: bool, silent: bool = False) -> None:
        """silent: принимающий промолчал, решение принято по таймеру (для оператора)."""
        o = self.offer
        d, r, s = self.dealer, self.receiver, o.stake
        r_think = round(self.clock() - self.t0)          # сколько решал принимающий
        if self.passed:
            head = (f"{self.exchange_no + 1}. {P(SOLO)} → {P(d)} → {P(r)}. "
                    f"Третий назвал «{PILL_NAME[self.slide_first]}», {P(d)} назвал «{PILL_NAME[o.label]}», "
                    f"была {PILL_NAME[o.pill]}. ")
        else:
            head = (f"{self.exchange_no + 1}. {P(d)} → {P(r)}. "
                    f"Названо «{PILL_NAME[o.label]}», была {PILL_NAME[o.pill]}. ")
        hit = None
        if s == 0:
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

        applied = hit is not None
        fx = None
        if hit is not None:
            if hit == SOLO and o.pill == BITTER and not self.tolerance_used:
                self.tolerance_used = True
                self.seen_fx.add("tol")
                text = self._say("tol_take" if take else "tol_refuse", p=P(SOLO))
                applied, fx = False, "tol"
            elif self.shield[hit] > 0:
                self.shield[hit] = 0
                text = self._say("shield", p=P(hit))
                applied, fx = False, "shield"
        self.log.append(head + text)

        self.history.append(Exchange3(d, r, o.pill, o.label, take, o.auto, o.forced or s == 0, self.passed))
        self.tray[o.pill] -= 1
        self.exchange_no += 1

        if applied:
            self.charges[hit] = max(0, self.charges[hit] - s)
        killed = applied and self.charges[hit] == 0

        said = voice.after_exchange(self.op, voice.Ex(
            dealer=d, receiver=r, pill=o.pill, label=o.label, take=take, stake=s, auto=o.auto, silent=silent,
            forced=o.forced, void=(s == 0), hit=hit, applied=applied, fx=fx, killed=killed,
            charges=tuple(self.charges), max_charges=tuple(self.max_charges),
            round_no=self.round_no, exchange_no=self.exchange_no,
            d_think=o.think, r_think=r_think, tray=(self.tray[CLEAN], self.tray[BITTER]),
            hidden=self.hidden_public(),
        ))
        if killed:
            if hit == SOLO:
                self._end_round(SIDE_TEAM, taker=self.cur_ally, pending=said)
            else:
                self._ally_falls(hit, by_hit=True, ghost_ok=hit not in self.out, pending=said)
            return
        self._op(said)
        if applied and hit in ALLIES and self._door_due():
            self._open_door()
            return
        self._continue(True)

    # ------------------------------------------------------------------
    # Предметы
    # ------------------------------------------------------------------

    def can_use(self, i: int, kind: str) -> tuple[bool, str]:
        if self.over:
            return False, "Партия окончена."
        if self.phase not in ("deal", "decide"):
            return False, "Сейчас не время."
        if i not in (0, 1, 2) or not self.alive(i):
            return False, "Ты выбыл."
        actor = i == self.turn
        spectator = i == self.observer()
        if not actor and not (spectator and kind == "recorder"):
            return False, "Предметы только в свой ход."
        if self.count(i, kind) == 0:
            return False, "Такого предмета нет."
        if kind == "recorder":
            if self.phase != "decide":
                return False, "Диктофон нужен, когда таблетку уже протянули."
            if i in self.lied_for:
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
            if self.charges[i] >= self.max_charges[i]:
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
        hidden = self.hidden_for(i)
        public_hidden = self.hidden_public()
        spectator = i == self.observer() and i != self.turn
        self.items[i].remove(kind)
        self.used_by[i].add(kind)
        if not public_hidden:
            self.used_pub.add(kind)
        if not spectator:
            self.decision_no += 1                    # предмет даёт игроку новое время
        private = ""
        if kind == "recorder":
            self.lied_for[i] = self.offer.pill != self.offer.label
            private = self.recorder_text(i)
            if not public_hidden:
                if spectator:
                    self.seen_fx.add("ear")
                    self.log.append(f"Диктофон: {P(i)} слушает для напарника. Запись включена.")
                else:
                    self.log.append(f"Диктофон: {P(i)}. Запись включена.")
        elif kind == "seal":
            self.forced = True
            private = "Пункт 7: отказ в этой выдаче не предусмотрен."
            if not public_hidden:
                self.log.append(f"Пункт 7: {P(i)}. Отказ в этой выдаче не предусмотрен.")
        elif kind == "receipt":
            self.stake = 2
            private = "Расписка: ставка ×2."
            if not public_hidden:
                self.log.append(f"Расписка: {P(i)}. Ставка ×2. Подписано обеими сторонами.")
        elif kind == "carrot":
            self.charges[i] += 1
            private = "Морковь: рассудок +1."
            if not public_hidden:
                self.log.append(f"Морковь: {P(i)}. Рассудок +1. " + self._say("carrot"))
        elif kind == CUT_KEY:
            self.offer.stake = 0
            private = "Вырезанный пункт: последствия этой выдачи вырезаны."
            if not public_hidden:
                self.log.append(f"{ITEM_REAL[CUT_KEY]}: {P(i)}. Последствия этой выдачи вырезаны.")
        if public_hidden:
            self.log.append(self.rng.choice(HIDDEN_USE).format(p=P(i)))
        else:
            # когда каталог повреждён, реплика назвала бы предмет: оператор молчит
            self._op(voice.on_item(self.op, kind, i, self.exchange_no))
        if hidden:
            private = f"Это был предмет «{ITEM_REAL[kind]}». {private}"
        return Result(True, private=private if (hidden or kind == "recorder") else "")

    def recorder_text(self, i: int) -> str:
        v = self.lied_for.get(i)
        if v is None:
            return ""
        return "Диктофон: голос дрогнул. Ложь." if v else "Диктофон: голос ровный. Правда."

    # ------------------------------------------------------------------
    # Таймер и сдача
    # ------------------------------------------------------------------

    def auto_move(self, i: int) -> None:
        """Время вышло. Молчание принимающего считается согласием; выдающий не успел: таблетка наугад,
        слово честное. Два раза подряд: курс проигран (участник просто выбывает из курса)."""
        if self.over or i != self.turn:
            return
        if self.phase == "ghost":
            self._ghost_silence()
            return
        if self.phase == "door":
            self._door_timeout()
            return
        self.timeouts[i] += 1
        if self.timeouts[i] >= 2:
            self.log.append(
                f"Время вышло дважды подряд ({P(i)}). Молчание тоже ответ. {P(i)} выбывает из курса."
            )
            line = voice.on_event(self.op, "auto_double", i, self.exchange_no - 1)
            if i == SOLO:
                self.charges[SOLO] = 0
                self._end_round(SIDE_TEAM, taker=self.cur_ally, pending=line)
            else:
                self._ally_falls(i, by_hit=False, ghost_ok=False, pending=line)
            return
        was = self.phase
        if was == "deal":
            self.log.append(
                f"Время вышло ({P(i)}). Выдача автоматическая: таблетка наугад, слово честное."
            )
            bag = [CLEAN] * self.tray[CLEAN] + [BITTER] * self.tray[BITTER]
            pill = self.rng.choice(bag)
            self._tick_only(i)
            self._offer(pill, pill, auto=True)
        else:
            self.log.append(self._say("timeout_take", p=P(i)))
            self._tick_only(i)
            self._resolve(True, silent=True)

    def _tick_only(self, i: int) -> None:
        """То же, что _tick, но счётчик тайм-аутов остаётся: его обнуляет только настоящий ход."""
        keep = self.timeouts[i]
        self._tick(i)
        self.timeouts[i] = keep

    def surrender(self, i: int) -> None:
        """Выход из матча. Третий вышел: участники выиграли. Участник вышел: он выбывает совсем."""
        if self.over or i not in (0, 1, 2):
            return
        self.log.append(f"Досрочный выход: {P(i)}. Согласие остаётся в силе.")
        self._op(voice.on_event(self.op, "surrender", i, self.exchange_no - 1))
        if i == SOLO:
            self._finish(SIDE_TEAM)
            return
        if i in self.out:
            return
        self.out.add(i)
        if not self.alive(i):
            # уже выбыл; если висел его выбор, считаем это молчанием
            if self.phase == "ghost" and self.ghost_seat == i:
                self._ghost_silence()
            return
        if self.phase == "ghost" and self.ghost_for == i:
            # вышел тот, кому выбывший собирался что-то передать: участников не осталось
            self.charges[i] = 0
            self.items[i] = []
            self._end_round(SIDE_SOLO)
            return
        in_pair = i == self.cur_ally or (self.passed and i in (self.dealer, self.receiver))
        if in_pair and self.phase in ("deal", "decide"):
            self._ally_falls(i, by_hit=False, ghost_ok=False)      # обмен отменяется
            return
        self.charges[i] = 0                                         # второй участник вышел «из зала»
        self.items[i] = []
        if not self.allies_alive():
            self._end_round(SIDE_SOLO)
        elif self.phase == "door":
            self._door_abort()


# ======================================================================
# ИИ-места. Нужны, чтобы админ мог проверять режим в одиночку, и для подбора баланса.
# Ходит той же логикой, что и ИИ-соперник из engine.py: смотрит на пару «я и тот, с кем сейчас обмен».
# ======================================================================

_BOT_RNG = random.Random()


class _View:
    """Вид на партию глазами места me как на дуэль 0/1: движок ИИ из engine.py умеет только такое."""

    def __init__(self, g: Trio, me: int):
        self.g, self.me = g, me
        partner = g.receiver if me == g.dealer else g.dealer
        self.charges = [g.charges[me], g.charges[partner]]
        self.phase = g.phase
        self.offer = g.offer
        self.stake = g.stake
        self.forced = g.forced
        self.tray = g.tray
        self.lied = g.lied_for.get(me)
        self.history = []
        for x in g.history:
            if x.dealer == me:
                self.history.append(Exchange(0, x.pill, x.label, x.take, x.auto, x.forced))
            elif x.receiver == me:
                self.history.append(Exchange(1, x.pill, x.label, x.take, x.auto, x.forced))

    def can_use(self, _me: int, kind: str):
        return self.g.can_use(self.me, kind)

    def pills(self):
        return self.g.pills()


def bot_move3(g: Trio, me: int, rng: random.Random | None = None) -> Result:
    """Один шаг ИИ на месте me: ответ выбывшего, предмет, выдача или решение."""
    rng = rng or _BOT_RNG
    if me == SOLO and g.phase in ("deal", "decide") and g.turn == SOLO:
        try:                                    # умный Третий (boss.py); при любой ошибке ходит старая логика
            from boss import boss_move3
            res = boss_move3(g, me, rng)
            if res.ok:
                return res
        except Exception:
            import logging
            logging.getLogger(__name__).exception("ИИ-Третий упал, ходит запасная логика")
    if g.phase == "ghost":
        return g.ghost_choose(me, rng.random() < 0.8)
    if g.phase == "door":
        return g.door_choose(me, rng.random() < BOT_LEAVE)
    if g.phase == "decide" and g.turn == me and g.can_slide(me)[0] and rng.random() < BOT_SLIDE:
        return g.slide(me, rng.choice((CLEAN, BITTER)))
    act = bot_choose(_View(g, me), 0, rng)
    if act[0] == "use":
        res = g.use(me, act[1])
    elif act[0] == "deal":
        res = g.deal(me, act[1], act[2])
    else:
        res = g.answer(me, act[1])
    if not res.ok:                      # страховка, чтобы партия не встала
        if g.phase == "deal" and g.turn == me:
            pill = g.pills()[0]
            res = g.deal(me, pill, pill)
        elif g.phase == "decide" and g.turn == me:
            res = g.answer(me, True)
    return res
