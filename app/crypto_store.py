"""
Хранилище сессий браузера (cookies + localStorage) в SQLite.
Данные шифруются симметричным ключом Fernet перед записью на диск.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SessionStore:
    def __init__(self, db_path: str, fernet_key: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    profile_id TEXT PRIMARY KEY,
                    cookies_enc BLOB NOT NULL,
                    local_storage_enc BLOB NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def save_session(self, profile_id: str, cookies: list, local_storage: dict) -> None:
        """Шифрует и сохраняет cookies и localStorage для профиля."""
        cookies_enc = self._fernet.encrypt(json.dumps(cookies).encode("utf-8"))
        ls_enc = self._fernet.encrypt(json.dumps(local_storage).encode("utf-8"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (profile_id, cookies_enc, local_storage_enc, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    cookies_enc=excluded.cookies_enc,
                    local_storage_enc=excluded.local_storage_enc,
                    updated_at=excluded.updated_at
                """,
                (profile_id, cookies_enc, ls_enc, time.time()),
            )
            conn.commit()

    def load_session(self, profile_id: str) -> Optional[dict]:
        """Возвращает {"cookies": [...], "local_storage": {...}} либо None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cookies_enc, local_storage_enc FROM sessions WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        if not row:
            return None
        cookies_enc, ls_enc = row
        try:
            cookies = json.loads(self._fernet.decrypt(cookies_enc).decode("utf-8"))
            local_storage = json.loads(self._fernet.decrypt(ls_enc).decode("utf-8"))
        except InvalidToken:
            # Ключ шифрования изменился либо данные повреждены — считаем сессию невалидной
            return None
        return {"cookies": cookies, "local_storage": local_storage}

    def delete_session(self, profile_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
            conn.commit()


session_store = SessionStore(settings.DB_PATH, settings.FERNET_KEY)
