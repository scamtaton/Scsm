"""Долговечная копия состояния бота в Redis.

SQLite остаётся рабочей базой (весь код рейтинга синхронный). Redis хранит снимок того же формата,
что и state.json: реестр, архив и идущие партии. После перезапуска на пустом диске бот берёт
снимок отсюда. Модуль ничего не знает про Telegram и про игру: он только кладёт и достаёт словари.

Ключи (префикс REDIS_PREFIX, по умолчанию "plenka"):
    <p>:tables  players + archive: большие и редко меняются
    <p>:live    всё остальное (идущие партии, created_ts): маленькие и меняются на каждом ходу
    <p>:prev    последний снимок, в котором были идущие партии (как state.json.prev на диске)
    <p>:meta    отпечаток и время последней суточной копии (backup_meta.json на диске)
    <p>:owner   токен экземпляра, который сейчас пишет; другой экземпляр писать перестаёт

Правила, ради которых модуль написан так:
  * Писать можно только после успешного чтения (arm()). Если Redis был недоступен на старте,
    пустая база не затрёт хороший снимок.
  * Сбой Redis никогда не роняет бота: save() возвращает False, чтение повторяется раз в минуту.
  * Два экземпляра на одном префиксе (перекрытие при деплое) не затирают друг друга:
    запись идёт только у того, чей токен лежит в :owner.
  * URL с паролем нигде не пишется в журнал.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time

log = logging.getLogger("game.redis")

TABLE_KEYS = ("players", "archive")        # остальные ключи снимка уходят в :live
RETRY_SECONDS = 60.0                       # как часто пробовать читать, пока Redis недоступен
LOG_EVERY = 60.0                           # как часто повторять одно и то же предупреждение
PULL_TIMEOUT = 20.0
SAVE_TIMEOUT = 10.0


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(raw) -> dict | None:
    """None, если ключа нет. ValueError, если там мусор: молча перезаписывать такое нельзя."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("ожидался JSON-объект")
    return obj


def _sig(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


class RedisStore:
    def __init__(self, url: str, prefix: str = "plenka", *, client=None, timeout: float = 5.0):
        self.prefix = prefix
        self.token = secrets.token_hex(8)
        self.ready = False            # снимок прочитан и влит: только теперь можно писать
        self.fenced = False           # нас вытеснил другой экземпляр
        self.error = ""               # последняя ошибка, для статуса в админ панели
        self.saved_at = 0.0
        self._sigs: dict[str, bytes | None] = {"tables": None, "live": None}
        self._retry_at = 0.0
        self._last_log = 0.0
        self._lock = asyncio.Lock()
        if client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as e:
                raise RuntimeError(
                    "REDIS_URL задан, но пакет redis не установлен: pip install -r requirements.txt"
                ) from e
            # from_url только разбирает адрес и не ходит в сеть: неверный адрес падает здесь, сразу
            client = Redis.from_url(
                url, decode_responses=True, socket_timeout=timeout,
                socket_connect_timeout=timeout, health_check_interval=30,
            )
        self.r = client

    def k(self, name: str) -> str:
        return f"{self.prefix}:{name}"

    # ------------------------------------------------------------------ состояние

    def arm(self) -> None:
        """Вызывающий влил снимок в SQLite: с этого момента запись разрешена."""
        self.ready = True
        self.error = ""

    def may_retry(self) -> bool:
        return not self.ready and time.monotonic() >= self._retry_at

    def failed(self, what: str, exc: BaseException) -> None:
        """Запоминает ошибку и пишет в журнал не чаще раза в минуту."""
        self.error = f"{what}: {type(exc).__name__}"
        if not self.ready:
            self._retry_at = time.monotonic() + RETRY_SECONDS
        now = time.monotonic()
        if now - self._last_log >= LOG_EVERY:
            self._last_log = now
            log.warning("redis %s: %s: %s", what, type(exc).__name__, exc)

    def status(self) -> str:
        if self.fenced:
            return "пишет другой экземпляр бота (этот не пишет)"
        if not self.ready:
            return f"не подключён, пишем только на диск ({self.error or 'ждём'})"
        if self.error:
            return f"сбой записи ({self.error})"
        if self.saved_at:
            return f"ok, запись {time.strftime('%H:%M:%S', time.localtime(self.saved_at))}"
        return "ok"

    # ------------------------------------------------------------------ чтение при старте

    async def pull(self) -> tuple[dict | None, dict | None]:
        """Занимает :owner и читает снимок. Возвращает (снимок или None, кандидат для /restore или None).

        Ничего не разрешает писать: после того как вызывающий влил снимок в SQLite, он зовёт arm().
        Исключение = Redis недоступен или данные испорчены; писать нельзя."""
        return await asyncio.wait_for(self._pull(), PULL_TIMEOUT)

    async def _pull(self):
        await self.r.set(self.k("owner"), self.token)
        tables_raw, live_raw = await self.r.mget([self.k("tables"), self.k("live")])
        tables, live = _loads(tables_raw), _loads(live_raw)
        snap = None
        if tables is not None or live is not None:
            snap = {**(live or {}), **{k: (tables or {}).get(k, []) for k in TABLE_KEYS}}
        return snap, await self._previous(snap)

    async def _previous(self, snap: dict | None) -> dict | None:
        """Тот же приём, что stash_previous_state для диска: если в снимке есть идущие партии,
        прячем его в :prev, чтобы свежий автосейв не затёр его до /restore."""
        if snap and (snap.get("sessions") or snap.get("trio")):
            await self.r.set(self.k("prev"), _dumps(snap))
            return snap
        return _loads(await self.r.get(self.k("prev")))

    async def drop_prev(self) -> None:
        try:
            await asyncio.wait_for(self.r.delete(self.k("prev")), SAVE_TIMEOUT)
        except Exception as e:
            self.failed("drop_prev", e)

    # ------------------------------------------------------------------ запись

    async def save(self, b: dict) -> bool:
        """Кладёт в Redis те части снимка, что изменились. Никогда не бросает исключений.
        На простое (ничего не изменилось) в сеть не ходит вообще."""
        if not self.ready or self.fenced:
            return False
        try:
            return await asyncio.wait_for(self._save(b), SAVE_TIMEOUT)
        except Exception as e:                    # CancelledError сюда не попадает: это BaseException
            self.failed("save", e)
            return False

    async def _save(self, b: dict) -> bool:
        tables = {k: b[k] for k in TABLE_KEYS if k in b}
        live = {k: v for k, v in b.items() if k not in TABLE_KEYS}
        parts = {
            "tables": (_dumps(tables), _sig(_dumps(tables))),
            # created_ts меняется при каждой сборке и в отпечаток не входит, как и в автосейве на диск
            "live": (_dumps(live), _sig(json.dumps(
                {k: v for k, v in live.items() if k != "created_ts"}, sort_keys=True, ensure_ascii=False))),
        }
        async with self._lock:
            todo = [n for n, (_, s) in parts.items() if s != self._sigs[n]]
            if not todo:
                return True
            owner = await self.r.get(self.k("owner"))
            if owner is None:                      # Redis потерял данные: запись нужна заново и целиком
                await self.r.set(self.k("owner"), self.token)
                self._sigs = {"tables": None, "live": None}
                todo = list(parts)
            elif owner != self.token:
                self.fenced = True
                log.warning("redis: ключи %s:* занял другой экземпляр бота, этот писать перестаёт", self.prefix)
                return False
            for n in todo:
                text, s = parts[n]
                await self.r.set(self.k(n), text)
                self._sigs[n] = s
            self.saved_at, self.error = time.time(), ""
            return True

    # ------------------------------------------------------------------ суточная копия

    async def get_meta(self) -> dict | None:
        try:
            return _loads(await asyncio.wait_for(self.r.get(self.k("meta")), SAVE_TIMEOUT))
        except Exception as e:
            self.failed("get_meta", e)
            return None

    async def set_meta(self, meta: dict) -> None:
        try:
            await asyncio.wait_for(self.r.set(self.k("meta"), _dumps(meta)), SAVE_TIMEOUT)
        except Exception as e:
            self.failed("set_meta", e)

    async def close(self) -> None:
        try:
            closer = getattr(self.r, "aclose", None) or self.r.close
            await asyncio.wait_for(closer(), 3)
        except Exception:
            pass
