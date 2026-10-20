"""Тесты redis_store.py без сети: клиент подменён словарём в памяти.

Запуск из корня репозитория:  python -m unittest discover -s tests -v
"""
import asyncio
import json
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis_store                       # noqa: E402
from redis_store import RedisStore       # noqa: E402


class FakeRedis:
    """Только те вызовы, что использует RedisStore. fail: исключение для следующих вызовов."""

    def __init__(self):
        self.data, self.calls, self.fail, self.delay, self.closed = {}, [], None, 0, False

    async def _hit(self, op, *a):
        self.calls.append((op, *a))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

    async def get(self, k):
        await self._hit("get", k)
        return self.data.get(k)

    async def set(self, k, v):
        await self._hit("set", k)
        self.data[k] = v
        return True

    async def mget(self, keys, *rest):
        await self._hit("mget", *keys)
        return [self.data.get(k) for k in keys]

    async def delete(self, *keys):
        await self._hit("delete", *keys)
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def aclose(self):
        self.closed = True

    def ops(self, name):
        return [c for c in self.calls if c[0] == name]


def snap(sessions=None, ts=None, players=None):
    return {
        "kind": "plenka-backup", "version": 1, "created_ts": ts or time.time(),
        "players": players or [[1, "Игрок ✓", 1000, 3, 2]], "archive": [[1, "e1", 5]],
        "sessions": sessions or [], "trio": [],
    }


def live_game(gid="g1"):
    return {"gid": gid, "game": {"round_no": 1, "exchange_no": 2, "wins": [0, 0]}}


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, fake=None, **kw):
        fake = fake or FakeRedis()
        return RedisStore("rediss://default:SECRETPW@host:6379", "t", client=fake, **kw), fake

    async def armed(self, fake=None):
        st, fake = self.make(fake)
        await st.pull()
        st.arm()
        return st, fake


class TestWriteGate(Base):
    async def test_no_write_before_arm(self):
        st, fake = self.make()
        self.assertFalse(await st.save(snap()))
        self.assertEqual(fake.calls, [])

    async def test_failed_pull_keeps_gate_closed_and_good_snapshot_untouched(self):
        fake = FakeRedis()
        good, _ = await self.armed(fake)
        await good.save(snap())
        before = dict(fake.data)
        st, _ = self.make(fake)
        fake.fail = ConnectionError("down")
        with self.assertRaises(ConnectionError):
            await st.pull()
        st.failed("read", ConnectionError("down"))
        fake.fail = None
        self.assertFalse(await st.save(snap(players=[])))          # пустая база не должна ничего затереть
        self.assertEqual({k: v for k, v in fake.data.items() if k != "t:owner"},
                         {k: v for k, v in before.items() if k != "t:owner"})

    async def test_retry_is_throttled(self):
        st, _ = self.make()
        self.assertTrue(st.may_retry())
        st.failed("read", ConnectionError("x"))
        self.assertFalse(st.may_retry())
        st._retry_at = 0
        self.assertTrue(st.may_retry())
        st.arm()
        self.assertFalse(st.may_retry())

    async def test_corrupt_snapshot_is_not_overwritten(self):
        for bad in ("{not json", "[1, 2]"):
            fake = FakeRedis()
            fake.data["t:tables"] = bad
            st, _ = self.make(fake)
            with self.assertRaises(ValueError):
                await st.pull()
            self.assertFalse(st.ready)
            self.assertFalse(await st.save(snap()))
            self.assertEqual(fake.data["t:tables"], bad)


class TestRoundTrip(Base):
    async def test_empty_redis(self):
        st, _ = self.make()
        self.assertEqual(await st.pull(), (None, None))

    async def test_save_then_pull_returns_same_snapshot(self):
        st, fake = await self.armed()
        b = snap(sessions=[live_game()])
        self.assertTrue(await st.save(b))
        st2, _ = self.make(fake)
        got, _prev = await st2.pull()
        self.assertEqual(got, b)
        self.assertIn("Игрок ✓", fake.data["t:tables"])            # юникод не превращается в \uXXXX

    async def test_only_live_present_gives_empty_tables(self):
        fake = FakeRedis()
        fake.data["t:live"] = json.dumps({"created_ts": 1.0, "sessions": [], "trio": []})
        st, _ = self.make(fake)
        got, _ = await st.pull()
        self.assertEqual((got["players"], got["archive"]), ([], []))

    async def test_idle_tick_costs_no_commands(self):
        st, fake = await self.armed()
        await st.save(snap(ts=1.0))
        n = len(fake.calls)
        self.assertTrue(await st.save(snap(ts=999.0)))             # изменился только created_ts
        self.assertEqual(len(fake.calls), n)

    async def test_only_changed_part_is_written(self):
        st, fake = await self.armed()
        await st.save(snap())
        fake.calls.clear()
        await st.save(snap(sessions=[live_game()]))                # ход в партии: меняется только :live
        self.assertEqual([c[1] for c in fake.ops("set")], ["t:live"])
        fake.calls.clear()
        await st.save(snap(sessions=[live_game()], players=[[1, "Игрок ✓", 1012, 4, 2]]))
        self.assertEqual([c[1] for c in fake.ops("set")], ["t:tables"])    # конец партии: меняется рейтинг

    async def test_redis_lost_its_data_gets_repopulated(self):
        st, fake = await self.armed()
        await st.save(snap())
        fake.data.clear()                                          # перезапуск Redis без persistence
        self.assertTrue(await st.save(snap(sessions=[live_game()])))
        self.assertEqual({"t:owner", "t:tables", "t:live"}, set(fake.data))
        self.assertEqual(json.loads(fake.data["t:tables"])["players"][0][1], "Игрок ✓")


class TestFencing(Base):
    async def test_second_instance_takes_over(self):
        fake = FakeRedis()
        a, _ = await self.armed(fake)
        await a.save(snap())
        b, _ = await self.armed(fake)                              # новый экземпляр при деплое
        fake.calls.clear()
        self.assertFalse(await a.save(snap(sessions=[live_game("old")])))
        self.assertTrue(a.fenced)
        self.assertEqual(fake.ops("set"), [])                      # старый ничего не записал
        self.assertTrue(await b.save(snap(sessions=[live_game("new")])))
        self.assertIn('"new"', fake.data["t:live"])
        fake.calls.clear()
        self.assertFalse(await a.save(snap(sessions=[live_game("x")])))
        self.assertEqual(fake.calls, [])                           # вытесненный больше в сеть не ходит


class TestFailures(Base):
    async def test_save_never_raises_and_retries_unchanged_parts(self):
        st, fake = await self.armed()
        fake.fail = ConnectionError("reset")
        self.assertFalse(await st.save(snap()))
        self.assertIn("сбой записи", st.status())
        fake.fail = None
        self.assertTrue(await st.save(snap()))                     # отпечаток не сдвинулся: пишет заново
        self.assertEqual(sorted(c[1] for c in fake.ops("set") if c[1] != "t:owner"), ["t:live", "t:tables"])
        self.assertTrue(st.status().startswith("ok"))

    async def test_hanging_redis_times_out(self):
        st, fake = await self.armed()
        fake.delay = 5
        old = redis_store.SAVE_TIMEOUT
        redis_store.SAVE_TIMEOUT = 0.05
        try:
            t = time.monotonic()
            self.assertFalse(await st.save(snap()))
            self.assertLess(time.monotonic() - t, 1)
        finally:
            redis_store.SAVE_TIMEOUT = old

    async def test_password_never_logged(self):
        st, fake = await self.armed()
        fake.fail = ConnectionError("Error 111 connecting to host:6379")
        with self.assertLogs("game.redis", level=logging.WARNING) as cm:
            await st.save(snap())
        self.assertNotIn("SECRETPW", "\n".join(cm.output))
        self.assertNotIn("SECRETPW", st.status())

    async def test_warning_is_rate_limited(self):
        st, fake = await self.armed()
        fake.fail = ConnectionError("x")
        with self.assertLogs("game.redis", level=logging.WARNING) as cm:
            for i in range(5):
                await st.save(snap(ts=i, sessions=[live_game(str(i))]))
        self.assertEqual(len(cm.output), 1)


class TestPrevAndMeta(Base):
    async def test_live_games_are_stashed_and_survive_next_autosave(self):
        fake = FakeRedis()
        a, _ = await self.armed(fake)
        await a.save(snap(sessions=[live_game()]))                 # бот упал с идущей партией
        b, _ = self.make(fake)
        got, prev = await b.pull()
        self.assertEqual(prev, got)
        self.assertEqual(len(prev["sessions"]), 1)
        b.arm()
        await b.save(snap())                                       # новый автосейв затирает :live
        self.assertEqual(json.loads(fake.data["t:live"])["sessions"], [])
        c, _ = self.make(fake)
        got2, prev2 = await c.pull()                               # но :prev остался
        self.assertEqual(got2["sessions"], [])
        self.assertEqual(len(prev2["sessions"]), 1)

    async def test_drop_prev(self):
        fake = FakeRedis()
        a, _ = await self.armed(fake)
        await a.save(snap(sessions=[live_game()]))
        b, _ = self.make(fake)
        await b.pull()
        b.arm()
        await b.drop_prev()                                        # /restore выполнен
        self.assertNotIn("t:prev", fake.data)
        await b.save(snap())                                       # автосейв: идущих партий уже нет в :live
        c, _ = self.make(fake)
        self.assertIsNone((await c.pull())[1])

    async def test_meta_roundtrip_and_errors(self):
        st, fake = await self.armed()
        self.assertIsNone(await st.get_meta())
        await st.set_meta({"fp": "abc", "ts": 5.0})
        self.assertEqual(await st.get_meta(), {"fp": "abc", "ts": 5.0})
        fake.fail = ConnectionError("x")
        self.assertIsNone(await st.get_meta())                     # не бросает
        await st.set_meta({"fp": "z"})


class TestMisc(Base):
    async def test_status_lifecycle(self):
        st, fake = self.make()
        self.assertIn("не подключён", st.status())
        await st.pull()
        st.arm()
        self.assertEqual(st.status(), "ok")
        await st.save(snap())
        self.assertTrue(st.status().startswith("ok, запись"))
        other, _ = self.make(fake)
        await other.pull()
        await st.save(snap(sessions=[live_game()]))
        self.assertIn("другой экземпляр", st.status())

    async def test_close_with_old_and_new_clients(self):
        st, fake = self.make()
        await st.close()
        self.assertTrue(fake.closed)

        class Old:
            closed = False

            async def close(self):
                Old.closed = True
        st2 = RedisStore("x", client=Old())
        await st2.close()
        self.assertTrue(Old.closed)

    def test_missing_package_gives_clear_error(self):
        sys.modules["redis"] = None                                # импорт redis.asyncio -> ImportError
        sys.modules["redis.asyncio"] = None
        try:
            with self.assertRaises(RuntimeError) as cm:
                RedisStore("redis://localhost")
            self.assertIn("pip install", str(cm.exception))
        finally:
            sys.modules.pop("redis", None)
            sys.modules.pop("redis.asyncio", None)


if __name__ == "__main__":
    unittest.main()
