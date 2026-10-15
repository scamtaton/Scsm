"""Проверка движка: инварианты на тысячах случайных партий + оценка баланса.

Запуск: python test_engine.py
"""
import random
import statistics
from collections import Counter

from engine import Game, INVENTORY_CAP


def check_invariants(g: Game) -> None:
    assert g.turn in (0, 1)
    assert g.shifts[0] <= 2 and g.shifts[1] <= 2
    if g.over:
        assert g.winner in (0, 1)
        return
    assert 1 <= g.shift <= 3
    for p in (0, 1):
        assert 1 <= g.hp[p] <= g.max_hp, g.hp
        assert len(g.items[p]) <= INVENTORY_CAP
    assert 0 <= g.ptr < len(g.slots)
    pairs = {}
    for s in g.slots:
        if s.pair < 0:
            assert not s.copy
        else:
            pairs.setdefault(s.pair, []).append(s)
    for p, members in pairs.items():
        assert len(members) == 2 and sum(1 for m in members if m.copy) == 1
    assert len({s.uid for s in g.slots}) == len(g.slots)
    # одиночные лица не совпадают ни с какими другими
    faces = [s.face for s in g.slots if s.pair < 0]
    assert len(faces) == len(set(faces))


def random_policy(g: Game, i: int, rng: random.Random) -> None:
    """Случайные легальные действия, в том числе все предметы."""
    for _ in range(4):
        if rng.random() < 0.5:
            break
        kinds = [k for k in set(it.kind for it in g.items[i]) if g.can_use(i, k)[0]]
        if not kinds:
            break
        k = rng.choice(kinds)
        arg = None
        if k == "check":
            arg = rng.choice(g.check_options())[0]
        elif k == "swap":
            arg = rng.choice(g.swap_options())
        elif k == "copy":
            arg = rng.choice(g.copy_options(i))
        res = g.use(i, k, arg)
        assert res.ok, res.err
        check_invariants(g)
    (g.take if rng.random() < 0.5 else g.give)(i)


def smart_policy(g: Game, i: int, rng: random.Random) -> None:
    """Простая разумная стратегия: читает публичную структуру пар и пользуется предметами."""
    o = 1 - i

    def use(kind, arg=None):
        res = g.use(i, kind, arg)
        assert res.ok, res.err

    if g.can_use(i, "heal")[0]:
        use("heal")
    if g.can_use(i, "void")[0] and g.hp[i] <= 2:
        use("void")
    if g.can_use(i, "noise")[0]:
        use("noise")
    if g.can_use(i, "copy")[0]:
        use("copy", g.copy_options(i)[0])
    status = g.next_status()
    if status == "unknown" and g.can_use(i, "check")[0]:
        s = g.slots[g.ptr]
        use("check", s.pair)
    uid = g.slots[g.ptr].uid
    know = g.known[i].get(uid)
    if know is not None:
        status = "copy" if know else "clean"
    if status == "clean" and g.can_use(i, "hold")[0]:
        use("hold")
    if status == "clean":
        g.take(i)
    elif status == "copy":
        g.give(i)
    else:
        (g.take if g.hp[i] > g.hp[o] else g.give)(i)


def play(policies, seed, first=None):
    rng = random.Random(seed)
    g = Game(["A", "B"], first=first, rng=random.Random(seed + 1))
    n = 0
    while not g.over:
        i = g.turn
        policies[i](g, i, rng)
        check_invariants(g)
        n += 1
        assert n < 3000, "партия не заканчивается"
    return g, n


def main():
    # 1. инварианты: случайные игроки, все предметы
    lens = []
    for seed in range(4000):
        g, n = play([random_policy, random_policy], seed)
        lens.append(n)
    print(f"случайные игроки: 4000 партий, ходов в среднем {statistics.mean(lens):.1f}, максимум {max(lens)}")

    # 2. таймауты: два подряд = проигранная смена
    g = Game(["A", "B"], first=0, rng=random.Random(5))
    g.timeouts[0] = 1
    g.auto_move(0)
    assert g.shifts == [0, 1] and g.shift == 2 and g.turn == 0, (g.shifts, g.shift, g.turn)
    g = Game(["A", "B"], first=0, rng=random.Random(5))
    g.auto_move(0)
    assert g.shifts == [0, 0] and g.timeouts[0] == 1
    print("таймауты: ок")

    # 3. навык: умный против случайного
    wins = Counter()
    for seed in range(3000):
        smart_idx = seed % 2
        pol = [smart_policy, random_policy] if smart_idx == 0 else [random_policy, smart_policy]
        g, _ = play(pol, 10_000 + seed)
        wins["smart" if g.winner == smart_idx else "random"] += 1
    print(f"умный против случайного: умный выигрывает {wins['smart'] / 3000:.1%}")

    # 4. зеркало: умный против умного, преимущество первого хода в первой смене
    first_wins = 0
    lens = []
    N = 4000
    for seed in range(N):
        g, n = play([smart_policy, smart_policy], 50_000 + seed, first=0)
        first_wins += g.winner == 0
        lens.append(n)
    print(f"зеркало: выигрывает первый ход {first_wins / N:.1%}, решений за матч в среднем {statistics.mean(lens):.1f}")

    # 5. сколько смен играется
    c = Counter()
    for seed in range(2000):
        g, _ = play([smart_policy, smart_policy], 90_000 + seed)
        c[tuple(sorted(g.shifts))] += 1
    print("счёт матчей (зеркало):", dict(c))
    print("OK")


if __name__ == "__main__":
    main()
