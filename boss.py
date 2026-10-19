"""ИИ-Третий («босс»): играет за место 0 в режиме «Третий за столом», когда против него двое людей.
Про Telegram здесь ничего не известно. Подключается из trio.bot_move3.

Как думает.
  1. Модель. Партия сворачивается в маленькое состояние: рассудок троих, «Привыкание», щиты, лоток,
     чья очередь выдавать. Дальше полный перебор на DEPTH выдач вперёд.
  2. Равновесие. Каждая выдача это игра с нулевой суммой: выдающий выбирает (таблетка, слово), принимающий
     видит только слово и решает. Такая игра решается точно (симплекс), её решение даёт смешанную стратегию:
     когда блефовать и как часто. Поэтому босса нельзя «прочитать» (он не лжёт всегда и не говорит правду всегда).
  3. Подстройка. По вскрытой истории матча считаем, как конкретный человек отказывается и как врёт.
     Чем больше данных, тем сильнее босс смещается от равновесия к лучшему ответу на привычки человека,
     но никогда не уходит в чистый ответ: люди адаптируются.
  4. Предметы. Диктофон, когда он стоит своих денег (ценность информации), Вырезанный пункт, когда спасает,
     Морковь сразу, Пункт 7 и Расписка, если игра с ними даёт больше, чем без.
  5. Честность. С 3 курса названия предметов не видны никому. Босс тоже играет вслепую: нажимает случайную
     кнопку из тех, что у него есть, и узнаёт предмет только после применения (как человек).

Числа в одном месте: словарь PARAMS.
"""
from __future__ import annotations

import math
import random
import time
from itertools import product

from engine import BITTER, CLEAN, CUT_FROM_ROUND, CUT_KEY, ITEM_KEYS, Result
from trio import ALLIES, SOLO, A, B

PARAMS = {
    "depth": 3,            # сколько выдач считаем вперёд
    "budget": 0.30,        # секунд на одно решение, потом считаем мельче
    "terminal": 12.0,      # цена конца курса
    "tol_w": 0.8,          # сколько рассудка стоит «Привыкание»
    "sh_w": 0.8,           # сколько рассудка стоит щит участника
    "dead_w": 0.3,         # бонус за выбывшего участника
    "near_death": 0.5,     # штраф, когда Третий в одном ударе от конца
    "near_kill": 0.25,     # бонус, когда участник в одном ударе от конца
    "item_cost": 0.2,      # цена предмета, чтобы не тратить впустую
    "rec_thr": 0.2,        # ценность информации, с которой Диктофон включается
    "cut_thr": 0.15,       # выгода, с которой тратится Вырезанный пункт
    "exploit_d": 0.6,      # потолок подстройки, когда босс выдаёт
    "exploit_r": 0.7,      # потолок подстройки, когда босс решает
    "prior_w": 3.0,        # вес равновесия как «предыдущих наблюдений»
    "k_br": 3.0,           # резкость лучшего ответа при неуверенности
    "blind_try": 0.05,     # какая ожидаемая выгода нужна, чтобы нажать неизвестную кнопку
}
P = PARAMS
_RNG = random.Random()


# ======================================================================
# Матричные игры
# ======================================================================

def _one(n: int, i: int) -> list[float]:
    v = [0.0] * n
    v[i] = 1.0
    return v


def _norm(v: list[float]) -> list[float]:
    v = [max(0.0, x) for x in v]
    s = sum(v)
    return [x / s for x in v] if s > 0 else [1.0 / len(v)] * len(v)


def solve(M: list[list[float]]) -> tuple[float, list[float], list[float]]:
    """Игра с нулевой суммой: строки максимизируют, столбцы минимизируют.
    Возвращает (цена игры, стратегия строк, стратегия столбцов)."""
    m, n = len(M), len(M[0])
    row_min = [min(r) for r in M]
    i0 = max(range(m), key=row_min.__getitem__)
    col_max = [max(M[i][j] for i in range(m)) for j in range(n)]
    j0 = min(range(n), key=col_max.__getitem__)
    if row_min[i0] >= col_max[j0] - 1e-9:                 # седловая точка: чистые стратегии
        return row_min[i0], _one(m, i0), _one(n, j0)
    lo = min(min(r) for r in M)
    c = 1.0 - lo                                          # сдвиг: все выплаты ≥ 1
    t = [[M[i][j] + c for j in range(n)] + [1.0 if k == i else 0.0 for k in range(m)] + [1.0]
         for i in range(m)]
    z = [-1.0] * n + [0.0] * m + [0.0]
    basis = [n + i for i in range(m)]
    for _ in range(80):                                   # симплекс, правило Бленда
        e = next((j for j in range(n + m) if z[j] < -1e-9), -1)
        if e < 0:
            break
        best, row = None, -1
        for i in range(m):
            if t[i][e] > 1e-9:
                ratio = t[i][-1] / t[i][e]
                if best is None or ratio < best - 1e-12 or (abs(ratio - best) <= 1e-12 and basis[i] < basis[row]):
                    best, row = ratio, i
        if row < 0:
            break
        pv = t[row][e]
        t[row] = [x / pv for x in t[row]]
        for i in range(m):
            if i != row and t[i][e] != 0.0:
                f = t[i][e]
                t[i] = [x - f * y for x, y in zip(t[i], t[row])]
        f = z[e]
        z = [x - f * y for x, y in zip(z, t[row])]
        basis[row] = e
    v = 1.0 / z[-1]
    y = [0.0] * n
    for i, bv in enumerate(basis):
        if bv < n:
            y[bv] = t[i][-1]
    q = _norm([yy * v for yy in y])
    x = _norm([z[n + i] * v for i in range(m)])
    return v - c, x, q


def _transpose(M: list[list[float]]) -> list[list[float]]:
    return [list(r) for r in zip(*M)]


# ======================================================================
# Модель партии
# ======================================================================
# Состояние: (рассудок Т, А, Б, Привыкание есть, щит А, щит Б, чистых, горьких, текущий участник, sub, solo_first)

POLICIES = [(True, True), (True, False), (False, True), (False, False)]   # (принять, если слово «чистая»; ... «горькая»)


def _take(label: str, pol: tuple) -> bool:
    return pol[0] if label == CLEAN else pol[1]


def _roles(st: tuple) -> tuple[int, int]:
    cur, sub, sf = st[8], st[9], st[10]
    return (SOLO, cur) if ((sub == 0) == bool(sf)) else (cur, SOLO)


def _state(g) -> tuple:
    return (g.charges[0], g.charges[A], g.charges[B], 0 if g.tolerance_used else 1,
            int(g.shield[A] > 0), int(g.shield[B] > 0), g.tray[CLEAN], g.tray[BITTER],
            g.cur_ally, g.sub, int(g.solo_first))


def _ws(g) -> float:
    s0 = max(1, g.max_charges[0])
    a0 = sum(g.max_charges[a] for a in ALLIES) or 4
    return max(0.3, a0 / (s0 + P["tol_w"]))


class _Search:
    def __init__(self, ws: float, depth: int):
        self.ws, self.depth = ws, depth
        self.deadline = time.perf_counter() + P["budget"]
        self.memo: dict = {}
        self.games: dict = {}

    def lin(self, st: tuple) -> float:
        S, a, b, tol, sa, sb = st[:6]
        T = P["terminal"]
        if S <= 0:
            return -T
        if a <= 0 and b <= 0:
            return T
        v = self.ws * (S + P["tol_w"] * tol)
        if S <= 1 and not tol:
            v -= P["near_death"]
        for c, sh in ((a, sa), (b, sb)):
            if c <= 0:
                v += P["dead_w"]
            else:
                v -= c + P["sh_w"] * sh
                if c == 1 and not sh:
                    v += P["near_kill"]
        return v

    def nxt(self, st: tuple, d: int, r: int, pill: str, take: bool, s: int) -> tuple:
        """Состояние после выдачи (как Trio._resolve + _advance)."""
        c = [st[0], st[1], st[2]]
        tol, sa, sb, tc, tb, cur, sub, sf = st[3], st[4], st[5], st[6], st[7], st[8], st[9], st[10]
        if s > 0:
            if take:
                hit = r if pill == BITTER else None
            else:
                hit = d if pill == BITTER else r
            if hit is not None:
                if hit == SOLO and pill == BITTER and tol:
                    tol = 0
                elif hit == A and sa:
                    sa = 0
                elif hit == B and sb:
                    sb = 0
                else:
                    c[hit] = max(0, c[hit] - s)
        if pill == CLEAN:
            tc -= 1
        else:
            tb -= 1
        if c[0] > 0 and (c[1] > 0 or c[2] > 0):
            alive = [x for x in ALLIES if c[x] > 0]
            if cur in alive and sub == 0:
                sub = 1
            else:
                sub = 0
                others = [x for x in alive if x != cur]
                cur = others[0] if others else alive[0]
        return (c[0], c[1], c[2], tol, sa, sb, tc, tb, cur, sub, sf)

    def leaf(self, st: tuple, depth: int) -> float:
        if st[0] <= 0:
            return -P["terminal"]
        if st[1] <= 0 and st[2] <= 0:
            return P["terminal"]
        if depth <= 0 or st[6] == 0 or st[7] == 0:      # глубина вышла или лоток перезарядится
            return self.lin(st)
        return self.value(st, depth)

    def value(self, st: tuple, depth: int) -> float:
        key = (st, depth)
        v = self.memo.get(key)
        if v is None:
            if time.perf_counter() > self.deadline:
                v = self.lin(st)
            else:
                d, r = _roles(st)
                v = self.boss_solve(self.matrix(st, d, r, 1, False, depth)[2], d)[0]
            self.memo[key] = v
        return v

    def matrix(self, st: tuple, d: int, r: int, stake: int, forced: bool, depth: int):
        """rows: что может выдать выдающий; pols: что может решить принимающий;
        M[строка][столбец]: ценность для Третьего; child[(таблетка, принял)]."""
        pills = [p for p, n in ((CLEAN, st[6]), (BITTER, st[7])) if n > 0]
        rows = [(p, l) for p in pills for l in (CLEAN, BITTER)]
        pols = [(True, True)] if forced else POLICIES
        child = {}
        for p in pills:
            for take in ((True,) if forced else (True, False)):
                child[(p, take)] = self.leaf(self.nxt(st, d, r, p, take, stake), depth - 1)
        M = [[child[(p, _take(l, pol))] for pol in pols] for (p, l) in rows]
        return rows, pols, M, child

    @staticmethod
    def boss_solve(M, d: int):
        """Игра глазами Третьего: если он выдаёт, он выбирает строки, иначе столбцы."""
        return solve(M) if d == SOLO else solve(_transpose(M))

    def root(self, st: tuple, stake: int, forced: bool):
        """Корневая игра: (цена, стратегия Третьего, стратегия человека, rows, pols, child)."""
        key = (st, stake, forced)
        got = self.games.get(key)
        if got is None:
            d, r = _roles(st)
            rows, pols, M, child = self.matrix(st, d, r, stake, forced, self.depth)
            val, xs, ys = self.boss_solve(M, d)
            got = (val, xs, ys, rows, pols, child)
            self.games[key] = got
        return got


# ======================================================================
# Привычки людей по вскрытой истории
# ======================================================================

def _refusals(g, ally: int) -> tuple[dict, float]:
    """Как человек отвечает на выдачи Третьего: по названному слову [отказов, всего]. Второй участник с весом 0,6."""
    ref = {CLEAN: [0.0, 0.0], BITTER: [0.0, 0.0]}
    n = 0.0
    for x in g.history:
        if x.dealer != SOLO or x.passed or x.forced or x.auto:
            continue
        w = 1.0 if x.receiver == ally else 0.6
        ref[x.label][0] += w * (not x.take)
        ref[x.label][1] += w
        n += w
    return ref, n


def _lies(g, ally: int) -> tuple[dict, float]:
    """Как человек врёт Третьему: по настоящей таблетке [соврал, выдач]."""
    lies = {CLEAN: [0.0, 0.0], BITTER: [0.0, 0.0]}
    n = 0.0
    for x in g.history:
        if x.receiver != SOLO or x.dealer not in ALLIES or x.passed or x.forced or x.auto:
            continue
        w = 1.0 if x.dealer == ally else 0.5
        lies[x.pill][0] += w * (x.label != x.pill)
        lies[x.pill][1] += w
        n += w
    return lies, n


def _rate(pair: list, prior: float, w: float) -> float:
    return (pair[0] + prior * w) / (pair[1] + w)


# ======================================================================
# Босс
# ======================================================================

class _Boss:
    def __init__(self, g, me: int, rng: random.Random):
        self.g, self.me, self.rng = g, me, rng
        self.hidden = g.hidden_for(me)
        self.S = _Search(_ws(g), P["depth"])
        self.st = _state(g)
        m = g.__dict__.get("_boss_mem")
        if m is None or m["round"] != g.round_no:
            m = {"round": g.round_no, "used": set(), "dec": -1, "failed": set()}
            g.__dict__["_boss_mem"] = m
        if m["dec"] != g.decision_no:
            m["dec"], m["failed"] = g.decision_no, set()
        self.mem = m

    # ---------------- предметы ----------------

    def _rule_ok(self, kind: str) -> bool:
        """Подходит ли предмет по правилам прямо сейчас (без взгляда на то, что на руках)."""
        g, me = self.g, self.me
        if g.over or not g.alive(me):
            return False
        if kind == "recorder":
            return g.phase == "decide" and me not in g.lied_for
        if kind == "seal":
            return g.phase == "deal" and not g.forced and g.stake != 2
        if kind == "receipt":
            return g.phase == "deal" and g.stake != 2 and not g.forced
        if kind == "carrot":
            return g.charges[me] < g.max_charges[me]
        if kind == CUT_KEY:
            return g.phase == "decide" and g.offer is not None and g.offer.stake != 0
        return False

    def _kinds(self) -> list[str]:
        """Какие предметы вообще могут быть у босса, исходя из того, что он знает."""
        g, me = self.g, self.me
        if not self.hidden:
            return [k for k in ITEM_KEYS + [CUT_KEY] if g.can_use(me, k)[0]]
        if not g.items[me]:
            return []
        pool = list(ITEM_KEYS) + ([CUT_KEY] if g.round_no >= CUT_FROM_ROUND else [])
        return [k for k in pool if k not in self.mem["used"]]

    def _use(self, kind: str) -> Result:
        res = self.g.use(self.me, kind)
        if res.ok:
            self.mem["used"].add(kind)
        return res

    def _items(self, gains: dict[str, float]) -> Result | None:
        """gains: выгода от каждого предмета, который сейчас подходит по правилам.
        Открытые названия: берём лучший с выгодой > 0. Вслепую: нажимаем случайную кнопку,
        если в среднем это выгодно (неподходящий предмет просто не сработает, ничего не стоит)."""
        g, me = self.g, self.me
        if not self.hidden:
            ok = {k: v for k, v in gains.items() if g.can_use(me, k)[0]}
            if ok:
                k = max(ok, key=ok.get)
                if ok[k] > 0:
                    return self._use(k)
            return None
        kinds = self._kinds()
        held = [k for k in g.items[me] if k not in self.mem["failed"]]
        if not kinds or not held:
            return None
        mean = sum(gains.get(k, 0.0) for k in kinds) / len(kinds)
        if mean <= P["blind_try"]:
            return None
        slot = self.rng.choice(held)                      # выбор не зависит от того, что это за предмет
        res = self._use(slot)
        if res.ok:
            return res
        self.mem["failed"].add(slot)                      # «не сработало»: в этой точке выбора не повторяем
        return None

    # ---------------- босс выдаёт ----------------

    def deal(self) -> Result:
        g, me, S, st = self.g, self.me, self.S, self.st
        base = S.root(st, g.stake, g.forced)
        gains: dict[str, float] = {}
        cost = P["item_cost"]
        if self._rule_ok("carrot"):
            up = (st[0] + 1,) + st[1:]
            gains["carrot"] = S.root(up, g.stake, g.forced)[0] - base[0]
        if self._rule_ok("receipt"):
            gains["receipt"] = S.root(st, 2, False)[0] - cost - base[0]
        if self._rule_ok("seal"):
            gains["seal"] = S.root(st, g.stake, True)[0] - cost - base[0]
        res = self._items(gains)
        if res is not None:
            return res
        return self._pick(base)

    def _pick(self, game) -> Result:
        g, me = self.g, self.me
        val, xs, ys, rows, pols, child = game
        ally = g.receiver
        if g.forced:                                       # Пункт 7: отказа не будет, слово неважно
            pill = max({p for p, _ in rows}, key=lambda p: child[(p, True)])
            return g.deal(me, pill, self.rng.choice((CLEAN, BITTER)))
        # что делает человек: шанс отказа по слову, с равновесием в роли «предыдущих наблюдений»
        ref, n = _refusals(g, ally)
        w = P["prior_w"]
        r_lab = {}
        for lab in (CLEAN, BITTER):
            prior = sum(y for y, pol in zip(ys, pols) if not _take(lab, pol))
            r_lab[lab] = _rate(ref[lab], prior, w)
        br = max(range(len(rows)), key=lambda i: (1 - r_lab[rows[i][1]]) * child[(rows[i][0], True)]
                 + r_lab[rows[i][1]] * child[(rows[i][0], False)])
        lam = min(P["exploit_d"], n / (n + 6.0))
        probs = [(1 - lam) * x for x in xs]
        probs[br] += lam
        i = self.rng.choices(range(len(rows)), weights=probs)[0]
        pill, label = rows[i]
        res = g.deal(me, pill, label)
        if not res.ok:                                     # не должно случаться
            pill = g.pills()[0]
            res = g.deal(me, pill, pill)
        return res

    # ---------------- босс решает ----------------

    def decide(self) -> Result:
        g, me, S, st = self.g, self.me, self.S, self.st
        o = g.offer
        d = g.dealer
        if o.forced or o.stake == 0:
            return g.answer(me, True)
        lied = g.lied_for.get(me)
        lies, n = _lies(g, d)
        w = P["prior_w"]
        lie_b = _rate(lies[BITTER], 0.6, w)                # шанс назвать «чистая», когда горькая
        lie_c = _rate(lies[CLEAN], 0.25, w)                # шанс назвать «горькая», когда чистая
        tot = st[6] + st[7]
        pb = st[7] / tot if tot else 0.5
        if lied is not None:                               # Диктофон: таблетка известна точно
            q = 1.0 if ((o.label == BITTER) != lied) else 0.0
        else:
            say_b = (1 - lie_b) if o.label == BITTER else lie_b
            say_c = lie_c if o.label == BITTER else (1 - lie_c)
            den = pb * say_b + (1 - pb) * say_c
            q = pb * say_b / den if den > 0 else 0.5
        s = o.stake
        val = {(p, t): S.leaf(S.nxt(st, d, me, p, t, s), S.depth - 1) for p in (CLEAN, BITTER) for t in (True, False)}
        ev_t = q * val[(BITTER, True)] + (1 - q) * val[(CLEAN, True)]
        ev_r = q * val[(BITTER, False)] + (1 - q) * val[(CLEAN, False)]
        best = max(ev_t, ev_r)

        gains: dict[str, float] = {}
        if self._rule_ok("carrot"):
            up = (st[0] + 1,) + st[1:]
            vu = {(p, t): S.leaf(S.nxt(up, d, me, p, t, s), S.depth - 1) for p in (CLEAN, BITTER) for t in (True, False)}
            eu = max(q * vu[(BITTER, t)] + (1 - q) * vu[(CLEAN, t)] for t in (True, False))
            gains["carrot"] = eu - best
        if lied is None and self._rule_ok("recorder"):
            info = q * max(val[(BITTER, True)], val[(BITTER, False)]) + (1 - q) * max(val[(CLEAN, True)], val[(CLEAN, False)])
            gains["recorder"] = info - best - (P["rec_thr"] - 0.05)       # порог выше нуля: предмет не бесплатный
        if self._rule_ok(CUT_KEY):
            cut = {p: S.leaf(S.nxt(st, d, me, p, True, 0), S.depth - 1) for p in (CLEAN, BITTER)}
            gains[CUT_KEY] = q * cut[BITTER] + (1 - q) * cut[CLEAN] - best - P["cut_thr"]
        res = self._items(gains)
        if res is not None:
            return res

        # равновесие этой выдачи (Третий принимающий) и лучший ответ на привычки выдающего
        game = S.root(st, s, False)
        _, xs, _, rows, pols, _ = game
        p_nash = sum(x for x, pol in zip(xs, pols) if _take(o.label, pol))
        if lied is not None or q in (0.0, 1.0):
            p_take = 1.0 if ev_t > ev_r else (0.0 if ev_r > ev_t else 0.5)
        else:
            z = max(-30.0, min(30.0, (ev_t - ev_r) * P["k_br"]))
            p_br = 1 / (1 + math.exp(-z))
            lam = min(P["exploit_r"], n / (n + 4.0))
            p_take = (1 - lam) * p_nash + lam * p_br
        res = g.answer(me, self.rng.random() < p_take)
        if not res.ok:
            res = g.answer(me, True)
        return res


def boss_move3(g, me: int = SOLO, rng: random.Random | None = None) -> Result:
    """Один шаг ИИ-Третьего: предмет, выдача или решение."""
    rng = rng or _RNG
    if g.over or g.turn != me:
        return Result(False, "Сейчас не ход босса.")
    boss = _Boss(g, me, rng)
    if g.phase == "deal":
        return boss.deal()
    if g.phase == "decide":
        return boss.decide()
    return Result(False, "Сейчас не ход босса.")
