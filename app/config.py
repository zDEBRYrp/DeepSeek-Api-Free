"""
Конфигурация микросервиса.
Все параметры читаются из .env, при отсутствии ключа шифрования Fernet
он генерируется автоматически и дописывается в .env, чтобы не потерять
доступ к уже сохранённой сессии при перезапуске.

Селекторы ниже адаптированы под реальный интерфейс https://chat.deepseek.com
(проверены вручную в консоли браузера). Селекторы, помеченные VERIFY,
рекомендуется перепроверить на вашей версии UI (см. README / сообщение
ассистента), т.к. DeepSeek периодически меняет вёрстку.
"""

import json
import os
import random
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key

ENV_PATH = Path(os.getenv("ENV_FILE", ".env"))
if not ENV_PATH.exists():
    ENV_PATH.touch()

load_dotenv(dotenv_path=ENV_PATH)


def _get_or_create_fernet_key() -> str:
    """Возвращает ключ шифрования, генерируя новый при первом запуске."""
    key = os.getenv("FERNET_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        set_key(str(ENV_PATH), "FERNET_KEY", key)
        os.environ["FERNET_KEY"] = key
    return key


def _jitter(base: float, spread_ratio: float = 0.15) -> float:
    """
    Добавляет случайную погрешность к таймауту, чтобы поведение бота
    не было идентичным при каждом запуске.
    """
    delta = base * spread_ratio
    return base + random.uniform(-delta, delta)


class Settings:
    # --- Общие ---
    CHAT_URL: str = os.getenv("CHAT_URL", "https://chat.deepseek.com")
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
    COOKIE_FILE: str = os.getenv("COOKIE_FILE", "./cookies.txt")  # экспорт cookies из вашего браузера
    # Использовать реальный установленный браузер (меньше шансов попасть под
    # детект автоматизации), напр. "chrome". Пусто — встроенный Chromium Playwright.
    BROWSER_CHANNEL: str = os.getenv("BROWSER_CHANNEL", "chrome")
    # Опционально переопределить User-Agent (оставьте пустым для нативного).
    USER_AGENT: str = os.getenv("USER_AGENT", "")
    # Режим анти-детекта: "" (выкл), "rebrowser" (rebrowser-patches, патч Chromium).
    ANTIDETECT: str = os.getenv("ANTIDETECT", "")
    USER_DATA_DIR: str = os.getenv("USER_DATA_DIR", "./pw_profile")
    DB_PATH: str = os.getenv("DB_PATH", "./data/sessions.sqlite3")
    FERNET_KEY: str = _get_or_create_fernet_key()

    # --- Сетевой сервис ---
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # --- Таймауты (мс), с погрешностью применяются через get_timeout() ---
    LOGIN_WAIT_TIMEOUT_MS: int = int(os.getenv("LOGIN_WAIT_TIMEOUT_MS", "300000"))  # ~5 мин ожидания ручного входа
    NAV_TIMEOUT_MS: int = int(os.getenv("NAV_TIMEOUT_MS", "30000"))  # ~30 сек на загрузку страницы
    RESPONSE_TIMEOUT_MS: int = int(os.getenv("RESPONSE_TIMEOUT_MS", "300000"))  # ~5 мин на ответ (DeepThink может быть долгим)
    ACTION_TIMEOUT_MS: int = int(os.getenv("ACTION_TIMEOUT_MS", "10000"))  # ~10 сек на клики/ввод

    # --- Лимиты ---
    # Максимальная длина одного сообщения (символов). 0 = без ограничения
    # (DeepSeek сам обрабатывает очень длинные вставки, преобразуя их в .txt-вложение).
    MAX_MESSAGE_LENGTH: int = int(os.getenv("MAX_MESSAGE_LENGTH", "0"))

    # --- Режим памяти ---
    # "server" - контекст держит сам DeepSeek (один чат-тред, отправляем только
    #            последнее сообщение). Хорошо для простых клиентов и как дефолт в .env.example.
    # "client" - клиент сам шлёт всю историю; мост «сплющивает» её в ОДНО сообщение
    #            и каждый запрос начинает НОВЫЙ чат DeepSeek. Полностью stateless,
    #            нет утечки прошлых ответов. Нужен для opencode/Kilo Code.
    MEMORY_MODE: str = os.getenv("MEMORY_MODE", "server").strip().lower()
    # Рабочая директория проекта. Подсказывается модели, чтобы та использовала
    # абсолютные пути (независимо от cwd клиента, исполняющего команду).
    # Пусто = cwd процесса сервера (обычно папка, откуда запущен uvicorn).
    WORK_DIR: str = os.getenv("WORK_DIR", "").strip()
    MAX_FILES_PER_MESSAGE: int = int(os.getenv("MAX_FILES_PER_MESSAGE", "5"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))

    # --- CSS/Playwright-селекторы для https://chat.deepseek.com ---
    # Поле ввода сообщения
    SEL_MESSAGE_INPUT: str = os.getenv("SEL_MESSAGE_INPUT", "textarea")
    # Кнопка отправки (уникальный класс primary/filled/circle на DeepSeek)
    SEL_SEND_BUTTON: str = os.getenv(
        "SEL_SEND_BUTTON", "div.ds-button--primary.ds-button--filled.ds-button--circle"
    )
    # Кнопка прикрепления файла (на DeepSeek это сам input[type=file])
    SEL_ATTACH_BUTTON: str = os.getenv("SEL_ATTACH_BUTTON", "input[type='file']")
    SEL_FILE_INPUT: str = os.getenv("SEL_FILE_INPUT", "input[type='file']")
    # Блок ответа ассистента (основной контент сообщения модели)
    SEL_ASSISTANT_BLOCK: str = os.getenv(
        "SEL_ASSISTANT_BLOCK", "[class*='assistant-message-main-content']"
    )
    SEL_LAST_RESPONSE: str = os.getenv(
        "SEL_LAST_RESPONSE", "[class*='assistant-message-main-content']:last-of-type"
    )
    # Индикатор генерации ответа (loading и кнопка Stop)
    SEL_LOADING_INDICATOR: str = os.getenv(
        "SEL_LOADING_INDICATOR", "div[class*='loading'], div[class*='stop']"
    )
    # Cloudflare / анти-бот проверка ("капча"), которая блокирует ответ
    SEL_CHALLENGE_OVERLAY: str = os.getenv("SEL_CHALLENGE_OVERLAY", "#cf-overlay")
    CHALLENGE_TEXT: str = os.getenv("CHALLENGE_TEXT", "One more step before you proceed")
    # Переключатели DeepThink / Search (опционально, см. set_deep_think/set_search).
    # Если пусто - поиск кнопки по тексту (DEEP_THINK_LABELS / SEARCH_LABELS).
    SEL_DEEP_THINK_BUTTON: str = os.getenv("SEL_DEEP_THINK_BUTTON", "")
    SEL_SEARCH_BUTTON: str = os.getenv("SEL_SEARCH_BUTTON", "")
    DEEP_THINK_LABELS: list = [
        s.strip() for s in os.getenv(
            "DEEP_THINK_LABELS", "DeepThink,Deep Think,Глубокое мышление,Глубокое размышление"
        ).split(",") if s.strip()
    ]
    SEARCH_LABELS: list = [
        s.strip() for s in os.getenv(
            "SEARCH_LABELS", "Умный поиск,Search,Поиск,Веб-поиск"
        ).split(",") if s.strip()
    ]
    # Маркер того, что пользователь уже в чате (поле ввода присутствует)
    SEL_LOGGED_IN_MARKER: str = os.getenv("SEL_LOGGED_IN_MARKER", "textarea")
    # Кнопка создания нового чата (селектор опционально; если пусто - поиск по тексту)
    SEL_NEW_CHAT_BUTTON: str = os.getenv("SEL_NEW_CHAT_BUTTON", "")
    # Возможные подписи кнопки "Новый чат" (для авто-поиска по живому DOM)
    NEW_CHAT_LABELS: list = [
        s.strip() for s in os.getenv(
            "NEW_CHAT_LABELS", "New chat,Новый чат,Создать чат,新对话,Nouvelle conversation"
        ).split(",") if s.strip()
    ]
    SEL_LOGIN_FORM_MARKER: str = os.getenv("SEL_LOGIN_FORM_MARKER", "form")  # VERIFY

    # --- Редактирование и регенерация (VERIFY: перепроверьте на вашей версии UI) ---
    # Обёртка сообщения - div с классом ds-message (роль определяем по наличию
    # внутри ds-assistant-message-main-content). data-message-role в UI нет.
    SEL_MESSAGE_ITEM: str = os.getenv(
        "SEL_MESSAGE_ITEM", "div[class*='ds-message']:not([class*='main-content'])"
    )
    SEL_MESSAGE_EDIT_BUTTON: str = os.getenv(
        "SEL_MESSAGE_EDIT_BUTTON", "button[class*='edit'], div[class*='edit'], [aria-label*='Edit']"
    )
    SEL_MESSAGE_EDIT_INPUT: str = os.getenv("SEL_MESSAGE_EDIT_INPUT", "textarea")
    SEL_MESSAGE_EDIT_SAVE: str = os.getenv(
        "SEL_MESSAGE_EDIT_SAVE", "button:has-text('Save'), button[type='submit']"
    )
    SEL_REGENERATE_BUTTON: str = os.getenv(
        "SEL_REGENERATE_BUTTON",
        "xpath=//div[@role='button' and .//*[local-name()='path'][starts-with(@d, 'M7.92136')]]",
    )

    # --- Валидация сессии ---
    VALIDATION_URL: str = os.getenv("VALIDATION_URL", "")

    # --- Внешний поиск (для флага search) ---
    SEARCH_API_URL: str = os.getenv("SEARCH_API_URL", "")
    SEARCH_API_KEY: str = os.getenv("SEARCH_API_KEY", "")
    SEARCH_RESULTS_LIMIT: int = int(os.getenv("SEARCH_RESULTS_LIMIT", "5"))

    # --- Deep think (префикс промпта, логика не меняется) ---
    DEEP_THINK_PREFIX: str = os.getenv(
        "DEEP_THINK_PREFIX",
        "Пожалуйста, поразмысли над вопросом пошагово, взвесь альтернативы и "
        "только затем дай итоговый развёрнутый ответ.\n\n",
    )

    # --- Системный промпт: вшивается в обычное сообщение ---
    # DeepSeek UI не имеет отдельного поля system prompt, поэтому инструкция
    # подставляется в начало пользовательского сообщения. {system} и {user} —
    # обязательные плейсхолдеры. Можно переопределить через .env.
    SYSTEM_PROMPT_TEMPLATE: str = os.getenv(
        "SYSTEM_PROMPT_TEMPLATE",
        "SYSTEM INSTRUCTION:\n{system}\n\n"
        "Follow the SYSTEM INSTRUCTION above for this and all subsequent "
        "responses. Now the user asks:\n\n{user}",
    )

    # --- Выбор режима через поле model ---
    # Маппинг model -> (deep_think, search). Позволяет выбирать режимы
    # Deepseek-Search / Deepseek-Think / Deepseek-Think_Search через model.
    # Формат env: "имя:d:d" через запятую (d = 0/1).
    MODE_TOGGLES: dict = {}
    for _pair in os.getenv(
        "MODE_TOGGLES",
        "deepseek-chat:0:0,deepseek-think:1:0,"
        "deepseek-search:0:1,deepseek-think-search:1:1",
    ).split(","):
        if ":" in _pair:
            _name, _d, _s = _pair.split(":")
            MODE_TOGGLES[_name.strip().lower()] = (bool(int(_d)), bool(int(_s)))

    @staticmethod
    def get_timeout(base_ms: int) -> float:
        """Таймаут в миллисекундах с небольшой случайной погрешностью."""
        return _jitter(base_ms)


settings = Settings()
