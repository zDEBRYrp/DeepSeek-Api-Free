"""
CLI для управления входом в DeepSeek.

Использование:
    python login.py                  # вход для профиля по умолчанию (USER_DATA_DIR)
    python login.py --profile acc1   # вход для профиля acc1 из profiles.json
    python login.py --check          # проверить, жив ли профиль по умолчанию
    python login.py --list           # показать все известные профили
    python login.py --manual         # инструкция ручного извлечения cookies (F12)

Вход выполняется в ВИДИМОМ браузере: скрипт открывает chat.deepseek.com,
ждёт появления поля ввода (маркер залогиненной сессии) и завершается —
постоянный профиль (USER_DATA_DIR) сохраняет сессию на диск сам.
Файл .env скрипт НЕ трогает.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

SYS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SYS_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(SYS_ROOT / ".env")

CHAT_URL = os.getenv("CHAT_URL", "https://chat.deepseek.com")
LOGIN_WAIT_S = int(os.getenv("LOGIN_WAIT_TIMEOUT_MS", "300000")) // 1000


def _env_default_profile() -> dict:
    return {
        "name": "default",
        "user_data_dir": os.getenv("USER_DATA_DIR", "./pw_profile"),
        "cookie_file": os.getenv("COOKIE_FILE", "./cookies.txt"),
    }


def _load_profiles() -> list:
    raw = os.getenv("DS_PROFILES", "").strip()
    pf = Path(os.getenv("PROFILES_FILE", "./profiles.json"))
    if not pf.is_absolute():
        pf = SYS_ROOT / pf
    if pf.exists():
        try:
            raw = pf.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Не удалось прочитать {pf}: {exc}")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError as exc:
            print(f"Невалидный JSON профилей: {exc}")
    return [_env_default_profile()]


def _resolve(name: str) -> dict:
    if not name or name == "default":
        return _env_default_profile()
    for p in _load_profiles():
        if isinstance(p, dict) and p.get("name") == name:
            base = _env_default_profile()
            base.update({k: v for k, v in p.items() if v})
            return base
    # Имя как путь к каталогу профиля.
    base = _env_default_profile()
    base.update({"name": name, "user_data_dir": name})
    return base


def cmd_list() -> int:
    print("Профили:")
    for p in _load_profiles():
        print(f"  - {p.get('name', '?')}: user_data_dir={p.get('user_data_dir', '?')}")
    d = _env_default_profile()
    print(f"Дефолт (.env): {d['user_data_dir']}")
    return 0


def cmd_check(profile: dict) -> int:
    udd = Path(profile["user_data_dir"])
    if not udd.is_absolute():
        udd = SYS_ROOT / udd
    ok = udd.exists() and any(udd.iterdir())
    print(f"Профиль '{profile['name']}': dir={udd} -> {'OK (непуст)' if ok else 'ПУСТ/НЕТ (нужен login)'}")
    return 0 if ok else 1


def cmd_login(profile: dict) -> int:
    from patchright.async_api import async_playwright

    import asyncio

    async def _run() -> int:
        udd = profile["user_data_dir"]
        print(f"Вход для профиля '{profile['name']}' (каталог: {udd})")
        print("Откроется ВИДИМОЕ окно браузера. Войдите в DeepSeek вручную.")
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=udd,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await ctx.new_page()
            await page.goto(CHAT_URL)
            deadline = time.time() + LOGIN_WAIT_S
            while time.time() < deadline:
                try:
                    if await page.query_selector("textarea"):
                        print("Маркер входа найден (textarea). Сессия сохранена в профиле.")
                        return 0
                except Exception:
                    pass
                await asyncio.sleep(2)
            print("Таймаут: вход не подтверждён.")
            return 1
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            await pw.stop()

    return asyncio.run(_run())


def cmd_manual() -> int:
    print(
        "Ручное извлечение cookies (F12):\n"
        f"  1. Откройте {CHAT_URL} и войдите.\n"
        "  2. F12 -> Application -> Cookies -> скопируйте нужные значения.\n"
        "  3. Положите их в файл формата Netscape/Mozilla cookies.txt\n"
        "     (см. COOKIE_FILE в .env) и перезапустите сервер —\n"
        "     мост подхватит сессию из файла при старте."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek Bridge login CLI")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list", dest="list_", action="store_true")
    ap.add_argument("--manual", action="store_true")
    args = ap.parse_args()
    if args.list_:
        return cmd_list()
    if args.manual:
        return cmd_manual()
    profile = _resolve(args.profile)
    if args.check:
        return cmd_check(profile)
    return cmd_login(profile)


if __name__ == "__main__":
    raise SystemExit(main())
