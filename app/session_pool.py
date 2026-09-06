"""
Пул браузерных сессий (профилей DeepSeek).

Идея украдена у notion2api (app/account_pool.py), но реализована лучше:
- asyncio-нативно (у них threading.Lock + time.sleep внутри лока — в async
  сервере это блокировало бы event loop; здесь блокировок event loop нет);
- единицей пула является живая BrowserSession, а не HTTP-клиент;
- пул из 1 профиля ведёт себя в точности как раньше (zero behavior change).

Профили задаются файлом profiles.json (рядом с .env) или переменной
DS_PROFILES (JSON-массив):
    [{"name": "acc1", "user_data_dir": "./pw_profile_1", "cookie_file": "./cookies1.txt"},
     {"name": "acc2", "user_data_dir": "./pw_profile_2"}]
Пусто -> один профиль из настроек (USER_DATA_DIR / COOKIE_FILE).
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("session_pool")


class PoolExhaustedError(Exception):
    """Все сессии в cooldown — клиенту следует ответить 429 + Retry-After."""

    def __init__(self, retry_after: int = 5):
        super().__init__("Все профили временно недоступны, повторите позже.")
        self.retry_after = max(1, int(retry_after))


@dataclass
class SessionProfile:
    name: str
    user_data_dir: str = ""
    cookie_file: str = ""


def load_profiles(
    settings_user_data_dir: str = "",
    settings_cookie_file: str = "",
) -> List[SessionProfile]:
    """Профили пула: profiles.json > DS_PROFILES > дефолт из настроек."""
    raw = ""
    profiles_path = Path(
        os.getenv("PROFILES_FILE", str(Path(".") / "profiles.json"))
    )
    if profiles_path.exists():
        try:
            raw = profiles_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Не удалось прочитать %s: %s", profiles_path, exc)
    if not raw:
        raw = os.getenv("DS_PROFILES", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON профилей пула: {exc}") from exc
        if not isinstance(data, list) or not data:
            raise ValueError("Профили пула должны быть непустым JSON-массивом.")
        profiles = []
        for idx, item in enumerate(data):
            if not isinstance(item, dict) or not item.get("user_data_dir"):
                raise ValueError(
                    f"Профиль пула[{idx}] обязан содержать user_data_dir."
                )
            profiles.append(
                SessionProfile(
                    name=str(item.get("name") or f"profile-{idx}"),
                    user_data_dir=str(item.get("user_data_dir") or ""),
                    cookie_file=str(item.get("cookie_file") or ""),
                )
            )
        return profiles
    return [
        SessionProfile(
            name="default",
            user_data_dir=settings_user_data_dir,
            cookie_file=settings_cookie_file,
        )
    ]


class SessionPool:
    """Round-robin по сессиям с cooldown после сбоев.

    Сессии создаются снаружи (чтобы пул не зависел от BrowserSession):
    pool = SessionPool(); pool.add("acc1", session1); ...
    """

    def __init__(self, cooldown_seconds: float = 30.0, max_wait_seconds: float = 15.0):
        self._names: List[str] = []
        self._sessions: list = []
        self._cooldown_until: List[float] = []
        self._in_use: set = set()  # индексы сессий, занятых генерацией прямо сейчас
        self._index = 0
        self._lock = asyncio.Lock()
        self._default_cooldown = max(1.0, float(cooldown_seconds))
        self._max_wait = max(0.0, float(max_wait_seconds))

    def add(self, name: str, session) -> None:
        self._names.append(name)
        self._sessions.append(session)
        self._cooldown_until.append(0.0)

    def __len__(self) -> int:
        return len(self._sessions)

    def primary(self):
        """Первая сессия — legacy-синглтон, когда пул из одного профиля."""
        return self._sessions[0] if self._sessions else None

    async def start_all(self) -> None:
        for name, sess in zip(self._names, self._sessions):
            try:
                await sess.start()
                logger.info("Профиль пула '%s' запущен.", name)
            except Exception as exc:  # один битый профиль не роняет сервис
                logger.warning("Профиль пула '%s' не стартовал: %s", name, exc)
                self._cooldown_until[self._names.index(name)] = (
                    time.time() + self._default_cooldown
                )

    async def close_all(self) -> None:
        for name, sess in zip(self._names, self._sessions):
            try:
                await sess.close()
            except Exception as exc:
                logger.warning("Ошибка закрытия профиля '%s': %s", name, exc)

    async def acquire(self, timeout: Optional[float] = None) -> object:
        """Свободная сессия (round-robin, мимо cooling и занятых).

        Параллельность = размер пула: разные запросы получают разные сессии
        («много вкладок»). Один профиль = строго последовательная работа.
        timeout=None -> self._max_wait. Исчерпание -> PoolExhaustedError (429).
        """
        if not self._sessions:
            raise PoolExhaustedError(5)
        limit = self._max_wait if timeout is None else max(0.0, float(timeout))
        deadline = time.time() + limit
        while True:
            async with self._lock:
                now = time.time()
                for _ in range(len(self._sessions)):
                    idx = self._index
                    self._index = (self._index + 1) % len(self._sessions)
                    if idx not in self._in_use and self._cooldown_until[idx] <= now:
                        self._in_use.add(idx)
                        return self._sessions[idx]
                cools = [ts - now for ts in self._cooldown_until if ts > now]
                min_cool = min(cools) if cools else 0.0
            remaining = deadline - time.time()
            if remaining <= 0:
                raise PoolExhaustedError(
                    retry_after=min_cool if min_cool > 0 else 5
                )
            # Ждём ВНЕ лока: либо истечения ближайшего cooldown, либо
            # освобождения занятой сессии (poll).
            step = min_cool if min_cool > 0 else 0.25
            await asyncio.sleep(max(0.05, min(step, remaining, 1.0)))

    async def release(self, session) -> None:
        """Вернуть сессию в ротацию после завершения запроса."""
        async with self._lock:
            try:
                idx = self._sessions.index(session)
            except ValueError:
                return
            self._in_use.discard(idx)

    async def mark_failed(
        self, session, cooldown_seconds: Optional[float] = None
    ) -> None:
        """Временно вывести сессию из ротации (сбой/разлогин/429 upstream)."""
        async with self._lock:
            try:
                idx = self._sessions.index(session)
            except ValueError:
                logger.warning("mark_failed: неизвестная сессия пула.")
                return
            cd = self._default_cooldown if cooldown_seconds is None else max(
                1.0, float(cooldown_seconds)
            )
            self._cooldown_until[idx] = time.time() + cd
            logger.warning(
                "Профиль пула '%s' в cooldown на %.0f c.", self._names[idx], cd
            )

    async def get_status_summary(self) -> dict:
        now = time.time()
        async with self._lock:
            active = sum(1 for ts in self._cooldown_until if ts <= now)
            return {
                "total": len(self._sessions),
                "active": active,
                "cooling": len(self._sessions) - active,
                "busy": len(self._in_use),
                "profiles": list(self._names),
            }
