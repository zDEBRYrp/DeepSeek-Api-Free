"""
Управление браузерной сессией через Playwright (Chromium).

Логика:
 - при первом запуске (нет сохранённых cookies/localStorage либо они невалидны)
   открывается видимый браузер, пользователь вручную логинится, после чего
   сессия сохраняется в зашифрованном виде в SQLite;
 - при последующих запусках сессия восстанавливается автоматически,
   валидность проверяется по маркеру интерфейса чата;
 - при истечении сессии повторно запускается ручной вход.

Селекторы адаптированы под https://chat.deepseek.com (см. app/config.py).
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse


def _text_delta(prev: str, new: str) -> str:
    """Возвращает часть new, которая не входит в prev (наиболее длинный общий
    префикс). Используется для потоковой отдачи приращений ответа."""
    if new.startswith(prev):
        return new[len(prev):]
    i = 0
    n = min(len(prev), len(new))
    while i < n and prev[i] == new[i]:
        i += 1
    return new[i:]

# --- Анти-детект браузер (patchright) ---
_antidetect = os.getenv("ANTIDETECT", "")
if _antidetect:
    try:
        from patchright.async_api import (
            async_playwright,
            Browser,
            BrowserContext,
            Page,
        )
        _USING_PATCHRIGHT = True
    except ImportError:
        logging.warning(
            "ANTIDETECT=%s, но patchright не установлен (pip install patchright). "
            "Используется обычный playwright.",
            _antidetect,
        )
        from playwright.async_api import (
            async_playwright,
            Browser,
            BrowserContext,
            Page,
        )
        _USING_PATCHRIGHT = False
else:
    from playwright.async_api import (
        async_playwright,
        Browser,
        BrowserContext,
        Page,
    )
    _USING_PATCHRIGHT = False

from app.config import settings
from app.crypto_store import session_store

logger = logging.getLogger("browser_session")

PROFILE_ID = "default"


class BrowserSessionError(Exception):
    pass


class BrowserSession:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._started = False

    # ---------- Жизненный цикл ----------

    async def start(self) -> None:
        if self._started:
            return
        self._playwright = await async_playwright().start()

        launch_kwargs = {
            "headless": settings.HEADLESS,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        context_kwargs = {}
        if settings.USER_AGENT:
            context_kwargs["user_agent"] = settings.USER_AGENT

        if settings.USER_DATA_DIR:
            # Постоянный профиль: реальные (в т.ч. httpOnly) куки и хранилище
            # сохраняются на диске между перезапусками сервиса.
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=settings.USER_DATA_DIR, **launch_kwargs, **context_kwargs
            )
            self._browser = self._context.browser
        else:
            if settings.BROWSER_CHANNEL:
                try:
                    self._browser = await self._playwright.chromium.launch(
                        channel=settings.BROWSER_CHANNEL, **launch_kwargs
                    )
                except Exception as exc:
                    logger.warning(
                        "Не удалось запустить браузер channel='%s' (%s). Пробуем встроенный Chromium.",
                        settings.BROWSER_CHANNEL,
                        exc,
                    )
                    self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            else:
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(**context_kwargs)

        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.get_timeout(settings.ACTION_TIMEOUT_MS))
        self._started = True
        asyncio.create_task(self._safe_restore_or_login())

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        elif self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._started = False

    # ---------- Доп. переключатели (опционально) ----------

    async def set_deep_think(self, enabled: bool = True) -> None:
        await self._toggle_button(settings.SEL_DEEP_THINK_BUTTON, enabled, settings.DEEP_THINK_LABELS)

    async def set_search(self, enabled: bool = True) -> None:
        await self._toggle_button(settings.SEL_SEARCH_BUTTON, enabled, settings.SEARCH_LABELS)

    async def _find_clickable_by_text(self, labels) -> Optional[Any]:
        for label in labels:
            try:
                locator = self._page.get_by_text(label, exact=False)
                if await locator.count() > 0:
                    return locator.first
            except Exception:
                pass
        return None

    async def _find_toggle(self, labels):
        """Находит div.ds-toggle-button по метке (DeepThink / Поиск).
        Важно: метка может лежать в родительском контейнере вместе с другими
        переключателями, поэтому ищем именно элемент с классом ds-toggle-button,
        содержащий нужный текст."""
        for label in labels:
            try:
                loc = self._page.locator("div[class*='ds-toggle-button']").filter(has_text=label)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        return None

    async def _toggle_button(self, selector: str, enabled: bool, labels=None) -> None:
        toggle_el = None
        try:
            if selector:
                toggle_el = await self._page.wait_for_selector(
                    selector, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
                )
        except Exception as exc:
            logger.warning("Селектор %s не найден (%s), пробуем поиск по тексту.", selector, exc)
            toggle_el = None

        if toggle_el is None and labels:
            toggle_el = await self._find_toggle(labels)

        if toggle_el is None:
            logger.warning("Не удалось найти кнопку (селектор=%s, метки=%s).", selector, labels)
            return

        try:
            for _ in range(4):
                state = await self._read_toggle_state(toggle_el)
                if state == enabled:
                    return
                # state is None (неизвестно) или противоположен нужному - кликаем
                await toggle_el.click()
                await asyncio.sleep(0.3)
            state = await self._read_toggle_state(toggle_el)
            if state != enabled:
                logger.warning(
                    "Не удалось установить переключатель в %s (состояние=%s).",
                    enabled, state,
                )
        except Exception as exc:
            logger.warning("Не удалось переключить кнопку: %s", exc)

    async def _read_toggle_state(self, toggle_el) -> Optional[bool]:
        """Читает состояние тумблера ds-toggle-button.
        Возвращает True/False или None, если определить не удалось."""
        try:
            is_pressed = await toggle_el.get_attribute("aria-pressed")
            if is_pressed is None:
                is_pressed = await toggle_el.get_attribute("data-pressed")
            if is_pressed is not None:
                return str(is_pressed).lower() in ("true", "1")
            cls = await toggle_el.get_attribute("class") or ""
            if "ds-toggle-button--selected" in cls:
                return True
            if "ds-toggle-button" in cls:
                # базовый класс есть, а selected - нет => выключен
                return False
        except Exception:
            pass
        return None

    # ---------- Создание нового чата ----------

    async def new_chat(self, return_url: bool = True) -> Optional[str]:
        async with self._lock:
            await self.ensure_logged_in()
            btn = None
            if settings.SEL_NEW_CHAT_BUTTON:
                try:
                    btn = await self._page.wait_for_selector(
                        settings.SEL_NEW_CHAT_BUTTON,
                        timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS),
                    )
                except Exception as exc:
                    logger.warning("Селектор нового чата не найден: %s", exc)
                    btn = None
            if btn is None:
                btn = await self._find_clickable_by_text(settings.NEW_CHAT_LABELS)
            if btn is None:
                await self._debug_dump("newchat")
                raise BrowserSessionError(
                    "Не удалось найти кнопку 'New chat' (проверьте SEL_NEW_CHAT_BUTTON / NEW_CHAT_LABELS)."
                )
            await btn.click()
            await asyncio.sleep(1.5)
            await self._wait_navigation_settled()
            logger.info("Создан новый чат. Текущий URL: %s", self._page.url)
            return self._page.url if return_url else None

    async def _wait_navigation_settled(self) -> None:
        try:
            await self._page.wait_for_load_state("networkidle", timeout=settings.get_timeout(8000))
        except Exception:
            pass

    # ---------- Работа с сессией ----------

    @staticmethod
    def _parse_cookies(raw: str) -> List[dict]:
        raw = raw.strip().lstrip("\ufeff")
        if not raw:
            return []

        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data
        except json.JSONDecodeError:
            pass

        netscape: List[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain = parts[0]
            path = parts[2]
            secure = parts[3].lower() == "true"
            expires = parts[4]
            name = parts[5]
            value = parts[6]
            http_only = False
            if domain.startswith("#HttpOnly_"):
                domain = domain[len("#HttpOnly_"):]
                http_only = True
            cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure,
                "httpOnly": http_only,
            }
            if expires.isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)
            netscape.append(cookie)
        if netscape:
            return netscape

        header = re.sub(r"(?i)^\s*cookie:\s*", "", raw).strip()
        header_cookies: List[dict] = []
        host = urlparse(settings.CHAT_URL).hostname or "chat.deepseek.com"
        for pair in re.split(r";\s*", header):
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            header_cookies.append(
                {"name": name, "value": value, "domain": "." + host, "path": "/"}
            )
        if header_cookies:
            return header_cookies

        return []

    async def _apply_cookie_file(self) -> bool:
        path = Path(settings.COOKIE_FILE)
        if not path.exists():
            return False
        raw = path.read_text(encoding="utf-8")
        cookies = self._parse_cookies(raw)
        if not cookies:
            logger.warning(
                "Файл cookies пуст или не распознан: %s. Первые 200 символов: %r",
                path,
                raw[:200],
            )
            return False
        await self._context.add_cookies(cookies)
        logger.info(
            "Загружено cookies из файла: %d шт. (имена: %s)",
            len(cookies),
            ", ".join(sorted({c.get("name", "?") for c in cookies})),
        )
        await self._page.goto(settings.CHAT_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))
        return True

    async def _apply_stored_session(self) -> bool:
        data = session_store.load_session(PROFILE_ID)
        if not data:
            return False

        await self._context.add_cookies(data["cookies"])
        await self._page.goto(settings.CHAT_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))

        if data["local_storage"]:
            await self._page.evaluate(
                "(items) => { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v); }",
                data["local_storage"],
            )
            await self._page.reload(timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))
        return True

    async def _is_session_valid(self) -> bool:
        try:
            if settings.VALIDATION_URL:
                resp = await self._page.goto(
                    settings.VALIDATION_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS)
                )
                if resp is None or resp.status >= 400:
                    return False
                await self._page.goto(settings.CHAT_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))

            try:
                await self._page.wait_for_selector(
                    settings.SEL_LOGGED_IN_MARKER, timeout=settings.get_timeout(8000)
                )
            except Exception:
                await self._page.wait_for_selector(
                    "textarea", timeout=settings.get_timeout(3000)
                )
            return True
        except Exception:
            try:
                url = self._page.url
                body = await self._page.query_selector("body")
                text = (await body.inner_text())[:400] if body else ""
                logger.warning(
                    "Сессия недействительна по маркеру '%s'. URL=%s; текст страницы: %r",
                    settings.SEL_LOGGED_IN_MARKER,
                    url,
                    text,
                )
            except Exception:
                pass
            return False

    async def _save_current_session(self) -> None:
        cookies = await self._context.cookies()
        local_storage = await self._page.evaluate(
            "() => { const o = {}; for (let i = 0; i < localStorage.length; i++) "
            "{ const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }"
        )
        session_store.save_session(PROFILE_ID, cookies, local_storage)
        logger.info("Сессия сохранена в зашифрованном хранилище.")

    async def _save_cookies_to_file(self) -> None:
        try:
            cookies = await self._context.cookies()
            Path(settings.COOKIE_FILE).write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Все cookies (%d шт.) сохранены в %s", len(cookies), settings.COOKIE_FILE)
        except Exception as exc:
            logger.warning("Не удалось сохранить cookies в файл: %s", exc)

    async def _manual_login(self) -> None:
        if settings.HEADLESS:
            raise BrowserSessionError(
                "Требуется ручной вход, но браузер запущен в headless-режиме. "
                "Перезапустите сервис с HEADLESS=false для первичной авторизации."
            )

        await self._page.goto(settings.CHAT_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))
        logger.info("Ожидание ручного входа пользователя в открытом окне браузера...")

        try:
            await self._page.wait_for_selector(
                settings.SEL_LOGGED_IN_MARKER,
                timeout=settings.get_timeout(settings.LOGIN_WAIT_TIMEOUT_MS),
            )
        except Exception:
            try:
                await self._page.wait_for_selector(
                    "textarea", timeout=settings.get_timeout(10000)
                )
                logger.warning(
                    "Маркер '%s' не найден, используем запасной детект (любой textarea).",
                    settings.SEL_LOGGED_IN_MARKER,
                )
            except Exception:
                raise BrowserSessionError(
                    "Не удалось детектировать успешный вход (нет поля ввода чата)."
                )
        logger.info("Вход выполнен, сохраняем сессию.")
        await self._save_current_session()
        await self._save_cookies_to_file()

    async def _restore_or_login(self) -> None:
        # 1. Если профиль браузера уже содержит активную сессию - используем её.
        try:
            await self._page.goto(
                settings.CHAT_URL, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS)
            )
            if await self._is_session_valid():
                logger.info("Активная сессия уже присутствует в профиле браузера.")
                await self._save_cookies_to_file()
                return
        except Exception as exc:
            logger.warning("Не удалось проверить текущую сессию: %s", exc)

        # 2. Пробуем файл cookies.
        if Path(settings.COOKIE_FILE).exists():
            try:
                if await self._apply_cookie_file():
                    if await self._is_session_valid():
                        logger.info("Сессия восстановлена из файла cookies.")
                        await self._save_current_session()
                        await self._save_cookies_to_file()
                        return
                    else:
                        logger.warning(
                            "Cookies из файла загружены, но сессия НЕ прошла валидацию. "
                            "Вероятно, экспорт не включает httpOnly-cookie с токеном сессии."
                        )
                else:
                    logger.warning("Файл cookies не загружен или пуст.")
            except Exception as exc:
                logger.warning("Не удалось применить cookies из файла: %s", exc)

        # 3. Пробуем сохранённую сессию из хранилища.
        restored = False
        try:
            restored = await self._apply_stored_session()
        except Exception as exc:
            logger.warning("Не удалось восстановить сохранённую сессию: %s", exc)

        if restored and await self._is_session_valid():
            logger.info("Сессия успешно восстановлена из хранилища.")
            return

        logger.info("Сохранённая сессия отсутствует или истекла - требуется ручной вход.")
        async with self._login_lock:
            await self._manual_login()

    async def _debug_dump(self, tag: str) -> None:
        try:
            url = self._page.url
            html = await self._page.content()
            Path(f"debug_{tag}.html").write_text(html, encoding="utf-8")
            logger.warning("DEBUG dump (%s): URL=%s; html length=%d", tag, url, len(html))
        except Exception as exc:
            logger.warning("DEBUG dump failed: %s", exc)

    async def _safe_restore_or_login(self) -> None:
        try:
            await self._restore_or_login()
        except Exception as exc:
            logger.error("Не удалось установить сессию: %s", exc)
            logger.error(
                "Сервис работает БЕЗ активной сессии. Проверьте cookies/аккаунт и перезапустите."
            )

    async def dump_page_html(self, path: str = "debug_page.html") -> str:
        if not self._page:
            raise BrowserSessionError("Страница не инициализирована.")
        url = self._page.url
        html = await self._page.content()
        Path(path).write_text(html, encoding="utf-8")
        logger.warning("DEBUG page dumped: URL=%s -> %s (%d bytes)", url, path, len(html))
        return url

    async def introspect_dom(self) -> dict:
        if not self._page:
            raise BrowserSessionError("Страница не инициализирована.")
        return await self._page.evaluate(
            """() => {
                const out = { url: location.href };
                const body = document.body;
                out.bodyTextLen = (body.innerText || '').length;
                out.hasPrompt = (body.innerText || '').includes('ZZUNIQUEMARKER123');
                out.roleButtons = Array.from(document.querySelectorAll('[role=button]')).map(b => ({
                    cls: (b.className || '').toString().slice(0, 160),
                    text: (b.innerText || '').slice(0, 40),
                    aria: b.getAttribute('aria-label')
                })).slice(0, 40);
                out.sendish = Array.from(document.querySelectorAll('button, [role=button], div')).filter(el => {
                    const c = (el.className || '').toString().toLowerCase();
                    const t = (el.innerText || '').toLowerCase();
                    return c.includes('send') || c.includes('submit') || c.includes('button')
                        || t.includes('отправить') || t.includes('send');
                }).map(el => ({
                    tag: el.tagName,
                    cls: (el.className || '').toString().slice(0, 160),
                    text: (el.innerText || '').slice(0, 40),
                    aria: el.getAttribute('aria-label')
                })).slice(0, 30);
                out.msglike = Array.from(document.querySelectorAll('div')).filter(el => {
                    const c = (el.className || '').toString().toLowerCase();
                    return c.includes('bubble') || c.includes('chat') || c.includes('msg')
                        || c.includes('content') || c.includes('user') || c.includes('assistant');
                }).map(el => ({
                    cls: (el.className || '').toString().slice(0, 160),
                    role: el.getAttribute('role'),
                    textLen: (el.innerText || '').length,
                    textHead: (el.innerText || '').slice(0, 60)
                })).slice(0, 40);
                return out;
            }"""
        )

    async def _is_challenge_present(self) -> bool:
        try:
            ov = await self._page.query_selector(settings.SEL_CHALLENGE_OVERLAY)
            if ov and await ov.is_visible():
                return True
            body = await self._page.query_selector("body")
            if body:
                text = (await body.inner_text()).lower()
                if settings.CHALLENGE_TEXT.lower() in text:
                    return True
        except Exception:
            pass
        return False

    async def ensure_logged_in(self) -> None:
        async with self._login_lock:
            if not self._started:
                await self.start()
                return
            if not await self._is_session_valid():
                await self._manual_login()

    # ---------- Ожидание ответа ----------

    @staticmethod
    def _is_control_label(text: str) -> bool:
        """Отбрасываем тексты, которые являются подписями UI-кнопок (Стоп/Копировать/...)."""
        low = text.lower()
        labels = ["остановить", "stop", "regenerate", "копировать", "copy", "поделиться",
                  "share", "retry", "повторить", "edit", "редактировать", "delete", "удалить"]
        return any(lbl in low for lbl in labels)

    # JS-извлечение контента без «шапок» code-блоков (язык + кнопки Копировать/Скачать).
    # Код оборачивается в markdown-забор (```lang ... ```), а подписи кнопок не попадают в ответ.
    EXTRACT_FN_SRC = r'''
    function extractContent(el) {
      if (!el) return '';
      var pres = Array.from(el.querySelectorAll('pre'));
      var fences = pres.map(function(pre) {
        var lang = '';
        var cur = pre.parentElement;
        for (var i = 0; i < 4 && cur; i++) {
          var hdr = cur.querySelector('[class*="header"], [class*="language"]');
          if (hdr) { lang = (hdr.textContent || '').replace(/Копировать|Скачать|Copy|Download|поделиться|share/gi, ' ').trim().split(/\s+/)[0] || ''; break; }
          cur = cur.parentElement;
        }
        if (!lang) { var sib = pre.previousElementSibling; if (sib) lang = (sib.textContent || '').replace(/Копировать|Скачать|Copy|Download/gi, ' ').trim().split(/\s+/)[0] || ''; }
        var code = (pre.innerText || pre.textContent || '').replace(/\s+$/, '');
        return '\n```' + lang + '\n' + code + '\n```\n';
      });
      var out = '';
      function walk(node) {
        if (node.nodeType === 3) { out += node.textContent; return; }
        if (node.nodeType !== 1) return;
        var tag = node.tagName.toLowerCase();
        if (tag === 'button' || (node.getAttribute && node.getAttribute('role') === 'button')) return;
        if (tag === 'pre') { var idx = pres.indexOf(node); out += (idx >= 0 ? fences[idx] : (node.innerText || '')); return; }
        if (tag === 'br') { out += '\n'; return; }
        if (node.children) {
          var preKids = Array.from(node.children).filter(function(c){ return c.tagName.toLowerCase() === 'pre'; });
          if (preKids.length) { preKids.forEach(function(pk){ var i = pres.indexOf(pk); out += (i >= 0 ? fences[i] : ''); }); return; }
        }
        var block = ['p','div','section','article','li','ul','ol','h1','h2','h3','h4','h5','h6','table','blockquote'].indexOf(tag) >= 0;
        node.childNodes.forEach(function(ch){ walk(ch); });
        if (block) out += '\n';
      }
      el.childNodes.forEach(function(ch){ walk(ch); });
      return out.replace(/\n{3,}/g, '\n\n').trim();
    }
    '''
    EXTRACT_CALL_JS = "(el) => { " + EXTRACT_FN_SRC + "\n return extractContent(el); }"

    @staticmethod
    def _looks_busy(text: str) -> bool:
        """True, если извлечённый текст похож на сообщение DeepSeek 'сервис занят'."""
        t = (text or "").lower().strip()
        if not t:
            return False
        markers = ["сервис занят", "занят обработкой", "одновременно поддерживается", "повтор через"]
        return any(m in t for m in markers) and len(t) < 600

    async def _extract_response_text(self, prompt: str = "") -> str:
        els = await self._page.query_selector_all(settings.SEL_ASSISTANT_BLOCK)
        if els:
            # берём последний НЕпустой блок (последний в DOM может быть пустым
            # "рабочим" контейнером во время генерации)
            candidates = []
            for el in els:
                try:
                    t = (await el.evaluate(self.EXTRACT_CALL_JS)).strip()
                except Exception:
                    try:
                        t = (await el.inner_text()).strip()
                    except Exception:
                        continue
                if t:
                    candidates.append(t)
            if candidates:
                text = candidates[-1]
                if (settings.CHALLENGE_TEXT.lower() not in text.lower()
                        and not self._is_control_label(text)
                        and not self._looks_busy(text)
                        and len(text) >= 5):
                    return text
            # блоки ассистента уже есть в DOM, но пока пусты -> ещё генерируется
            return ""
        try:
            result = await self._page.evaluate(
                """(args) => {
                    const prompt = args[0] || "";
                    const cands = Array.from(document.body.querySelectorAll('div, section, article, p'))
                        .filter(el => {
                            const t = (el.innerText || '').trim();
                            if (t.length < 10) return false;
                            if (el.querySelector('textarea, input, button')) return false;
                            if (getComputedStyle(el).display === 'none') return false;
                            if (prompt && t.includes(prompt)) return false;
                            const low = t.toLowerCase();
                            const labels = ['остановить','stop','regenerate','копировать','copy',
                                'поделиться','share','retry','edit','delete','удалить'];
                            if (labels.some(l => low.includes(l)) && t.length < 40) return false;
                            return true;
                        });
                    if (!cands.length) return '';
                    cands.sort((a, b) => {
                        const pos = a.compareDocumentPosition(b);
                        if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
                        if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
                        return 0;
                    });
                    return cands[cands.length - 1].innerText.trim();
                }""",
                [prompt],
            )
            if (result and settings.CHALLENGE_TEXT.lower() not in result.lower()
                    and not self._is_control_label(result) and len(result) >= 5):
                return result
        except Exception:
            return ""
        return ""

    async def _extract_stream_parts(self) -> tuple:
        """Возвращает (reasoning, content) последнего ответа ассистента.
        reasoning - блок рассуждений DeepThink (ds-think-content),
        content - итоговый ответ. Блок рассуждений - сосед блока ответа
        внутри общей обёртки сообщения (div.ds-message), поэтому ищем
        reasoning и content внутри последней обёртки ассистента."""
        try:
            res = await self._page.evaluate(
                "(el) => { " + self.EXTRACT_FN_SRC
                + """
                    const wrappers = Array.from(
                        document.querySelectorAll(
                            "div[class*='ds-message']:not([class*='main-content'])"
                        )
                    );
                    let last = null;
                    for (const w of wrappers) {
                        if (w.querySelector("[class*='ds-assistant-message-main-content']")) {
                            last = w;
                        }
                    }
                    if (!last) return {reasoning: "", content: ""};
                    const think = last.querySelector("[class*='think']");
                    const reasoning = think ? think.innerText.trim() : "";
                    const contentEl = last.querySelector(
                        "[class*='ds-assistant-message-main-content']"
                    );
                    const content = contentEl ? extractContent(contentEl).trim() : "";
                    return {reasoning, content};
                }"""
            )
            r = (res or {}).get("reasoning", "") or ""
            c = (res or {}).get("content", "") or ""
            if r and settings.CHALLENGE_TEXT.lower() in r.lower():
                r = ""
            if c and settings.CHALLENGE_TEXT.lower() in c.lower():
                c = ""
            # Сообщение "сервис занят" не должно попадать в ответ - ждём реальный ответ.
            if c and self._looks_busy(c):
                c = ""
            return r, c
        except Exception:
            return "", ""

    async def _stream_response(
        self,
        baseline_reasoning: str = "",
        baseline_content: str = "",
        require_change: bool = True,
    ):
        """Генератор, выдающий (kind, delta), где kind in {'reasoning','content'}.
        Сначала отдаются дельты рассуждений DeepThink, затем - ответа."""
        loop = asyncio.get_running_loop()
        # Для перегенерации (require_change=False) базовым считаем пустое -
        # DeepSeek может вернуть точно такой же ответ, и его тоже нужно отдать.
        base_r = "" if not require_change else baseline_reasoning
        base_c = "" if not require_change else baseline_content
        prev_r = ""
        prev_c = ""
        seen_new = False
        stable = 0
        await asyncio.sleep(1.5)
        deadline = loop.time() + settings.get_timeout(settings.RESPONSE_TIMEOUT_MS) / 1000.0
        while loop.time() < deadline:
            if await self._is_challenge_present():
                break
            r, c = await self._extract_stream_parts()
            dr = _text_delta(prev_r, r)
            dc = _text_delta(prev_c, c)
            # Отдаём дельту, только если контент реально изменился относительно
            # baseline (новая генерация) либо мы уже начали отдавать (seen_new).
            emit_r = dr if (r != base_r or seen_new) else ""
            emit_c = dc if (c != base_c or seen_new) else ""
            if emit_r:
                prev_r = r
                stable = 0
                seen_new = True
                yield "reasoning", emit_r
            if emit_c:
                prev_c = c
                stable = 0
                seen_new = True
                yield "content", emit_c
            # Завершаем, когда ответ (контент) уже начался и стабилизировался.
            # Важно: при DeepThink между фазой рассуждений и фазой ответа бывает
            # пауза, когда контент ещё пустой, а рассуждения уже стабильны -
            # тогда НЕ обрываем, иначе потеряем ответ.
            if (not emit_r and not emit_c and seen_new
                    and r == prev_r and c == prev_c and (prev_c or c)):
                stable += 1
                if stable >= 2:
                    break
            await asyncio.sleep(0.4)

    async def _extract_response_parts(self, prompt: str = "") -> tuple:
        """Возвращает (reasoning, content) последнего готового ответа.
        Блок рассуждений (ds-think-content) - сосед блока ответа внутри
        общей обёртки сообщения, поэтому ищем его внутри обёртки ассистента."""
        content = await self._extract_response_text(prompt)
        reasoning = ""
        try:
            wrappers = await self._page.query_selector_all(
                "div[class*='ds-message']:not([class*='main-content'])"
            )
            last_wrapper = None
            for w in wrappers:
                if await w.query_selector("[class*='ds-assistant-message-main-content']"):
                    last_wrapper = w
            if last_wrapper is not None:
                think = await last_wrapper.query_selector("[class*='think']")
                if think:
                    reasoning = (await think.inner_text()).strip()
        except Exception:
            reasoning = ""
        return reasoning, content

    async def _wait_response_complete(
        self, wait_new: bool = True, prompt: str = "", baseline_text: str = "",
        require_change: bool = True,
    ) -> str:
        # Не ждём явно индикатор загрузки (селектор может совпадать с постоянно
        # видимым элементом и "висеть" весь таймаут) - полагаемся на стабилизацию
        # текста ответа: когда DeepSeek дописал ответ, он перестаёт меняться.
        # При wait_new/require_change ждём именно НОВЫЙ ответ (текст, отличающийся
        # от baseline), чтобы не вернуть случайно старый стабильный ответ.
        # Для перегенерации require_change=False - ждём просто стабилизации
        # (регенерированный ответ может совпасть с прежним).
        loop = asyncio.get_running_loop()
        last = ""
        stable = 0
        saw_change = not require_change
        # небольшая пауза, чтобы ответ начал появляться в DOM
        await asyncio.sleep(4.0 if not require_change else 2.0)
        deadline = loop.time() + settings.get_timeout(settings.RESPONSE_TIMEOUT_MS) / 1000.0
        while loop.time() < deadline:
            text = await self._extract_response_text(prompt)
            # Фиксируем любое изменение относительно baseline (в т.ч. кратковременную
            # очистку ответа во время перегенерации). После этого ждём стабилизации,
            # даже если финальный текст совпал с baseline (regenerate вернул тот же ответ).
            if text and text != baseline_text:
                saw_change = True
            if saw_change and text and text == last:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = text
            await asyncio.sleep(1.0)

        if not last:
            if await self._is_challenge_present():
                await self._debug_dump("response")
                raise BrowserSessionError(
                    "DeepSeek требует проверку Cloudflare (капча). Пройдите её в окне "
                    "браузера (галочка 'Я не робот'), затем повторите запрос."
                )
            await self._debug_dump("response")
            raise BrowserSessionError("Не удалось найти ответ ассистента в интерфейсе.")
        reasoning, _ = await self._extract_response_parts(prompt)
        await self._save_cookies_to_file()
        return reasoning, last.strip()

    # ---------- Вложения ----------

    async def _attach_files(self, file_paths: List[str]) -> None:
        if not file_paths:
            return
        if len(file_paths) > settings.MAX_FILES_PER_MESSAGE:
            raise BrowserSessionError(
                f"Превышен лимит вложений: {len(file_paths)} > {settings.MAX_FILES_PER_MESSAGE}"
            )

        for path in file_paths:
            if not Path(path).exists():
                raise BrowserSessionError(f"Файл не найден: {path}")

        if settings.SEL_ATTACH_BUTTON != settings.SEL_FILE_INPUT:
            try:
                await self._page.click(
                    settings.SEL_ATTACH_BUTTON, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
                )
            except Exception:
                pass

        file_input = await self._page.wait_for_selector(
            settings.SEL_FILE_INPUT, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
        )
        await file_input.set_input_files(file_paths)

    # ---------- Действия ----------

    async def _submit_message(
        self,
        text: str,
        file_paths: Optional[List[str]] = None,
        deep_think: bool = False,
        search: bool = False,
        chat_id: Optional[str] = None,
    ) -> str:
        if chat_id:
            await self.ensure_chat(chat_id)
        await self.ensure_logged_in()

        # Всегда выставляем явное состояние (вкл/выкл), чтобы не было
        # "дрейфа": если предыдущий запрос включил DeepThink, а текущий нет -
        # тумблер должен гарантированно выключиться.
        await self.set_deep_think(deep_think)
        await self.set_search(search)

        # Если DeepSeek показал капчу Cloudflare - ждём, пока пользователь её пройдёт
        loop = asyncio.get_running_loop()
        challenge_deadline = loop.time() + 120
        if await self._is_challenge_present():
            logger.warning("Обнаружена капча Cloudflare - ожидаем прохождение пользователем...")
            while await self._is_challenge_present():
                if loop.time() > challenge_deadline:
                    raise BrowserSessionError(
                        "DeepSeek требует проверку Cloudflare (капча). Пройдите её в окне "
                        "браузера (галочка 'Я не робот'), затем повторите запрос."
                    )
                await asyncio.sleep(3)

        if settings.MAX_MESSAGE_LENGTH and len(text) > settings.MAX_MESSAGE_LENGTH:
            raise BrowserSessionError(
                f"Сообщение превышает допустимую длину ({settings.MAX_MESSAGE_LENGTH} символов)."
            )

        await self._attach_files(file_paths or [])

        try:
            input_box = await self._page.wait_for_selector(
                settings.SEL_MESSAGE_INPUT,
                state="attached",
                timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS),
            )
        except Exception:
            try:
                input_box = await self._page.wait_for_selector(
                    "textarea, [contenteditable='true'], [contenteditable=''], [role='textbox']",
                    state="attached",
                    timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS),
                )
            except Exception as exc:
                await self._debug_dump("input_not_found")
                raise BrowserSessionError("Не удалось найти поле ввода сообщения.") from exc

        await input_box.click()
        # DeepSeek - React-контролируемое поле. Обычные fill/type ставят
        # значение в DOM, но React-состояние остаётся пустым, т.к. React
        # кэширует старое значение через valueTracker и игнорирует input-
        # событие. Трюк: сбрасываем _valueTracker, чтобы React "заметил"
        # изменение и обновил состояние.
        try:
            await input_box.press("Control+a")
            await input_box.press("Delete")
        except Exception:
            pass
        await input_box.evaluate(
            """(el, val) => {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                const tracker = el._valueTracker;
                if (tracker) {
                    tracker.setValue('');
                }
                desc.set.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            text,
        )

        send_clicked = False
        try:
            await self._page.click(
                settings.SEL_SEND_BUTTON,
                timeout=settings.get_timeout(min(settings.ACTION_TIMEOUT_MS, 6000)),
            )
            send_clicked = True
        except Exception:
            pass

        if not send_clicked:
            try:
                await self._page.click(
                    "button[type='submit'], form button:last-of-type, button:has(svg), [aria-label*='Send'], [aria-label*='Отправить']",
                    timeout=settings.get_timeout(5000),
                )
                send_clicked = True
            except Exception:
                pass

        if not send_clicked:
            await input_box.press("Enter")

        # Запоминаем текущий последний ответ, чтобы при ожидании НЕ
        # вернуть случайно старый (стабильный) ответ из предыдущего
        # сообщения / предыдущего чата - ждём именно НОВЫЙ ответ.
        baseline_text = ""
        try:
            baseline_text = await self._extract_response_text(text)
        except Exception:
            baseline_text = ""
        return baseline_text

    async def send_message(
        self,
        text: str,
        file_paths: Optional[List[str]] = None,
        deep_think: bool = False,
        search: bool = False,
        chat_id: Optional[str] = None,
    ) -> str:
        async with self._lock:
            baseline = await self._submit_message(
                text, file_paths, deep_think, search, chat_id
            )
            for attempt in range(settings.MAX_RETRIES + 1):
                try:
                    return await self._wait_response_complete(
                        wait_new=True, prompt=text, baseline_text=baseline
                    )
                except Exception as exc:
                    if attempt == settings.MAX_RETRIES:
                        raise BrowserSessionError(f"Не удалось получить ответ от UI: {exc}") from exc
                    await asyncio.sleep(1)

    async def stream_message(
        self,
        text: str,
        file_paths: Optional[List[str]] = None,
        deep_think: bool = False,
        search: bool = False,
        chat_id: Optional[str] = None,
    ):
        """Генератор: отправляет сообщение и выдаёт (kind, delta), где
        kind in {'reasoning','content'}, по мере появления в интерфейсе."""
        async with self._lock:
            baseline = await self._submit_message(
                text, file_paths, deep_think, search, chat_id
            )
            async for kind, delta in self._stream_response(baseline_content=baseline):
                yield kind, delta

    async def regenerate_last(self, chat_id: Optional[str] = None) -> str:
        async with self._lock:
            if chat_id:
                await self.ensure_chat(chat_id)
            await self.ensure_logged_in()
            # Запоминаем текущий ответ, чтобы после перегенерации дождаться
            # ИМЕННО нового (изменившегося) варианта, а не вернуть старый.
            baseline_text = ""
            try:
                baseline_text = await self._extract_response_text()
            except Exception:
                pass
            # Кнопка "Повторить" под последним сообщением (иконка-кольцевая
            # стрелка). Кликаем через JS, чтобы не зависеть от видимости
            # панели действий (в DeepSeek она появляется только при наведении).
            clicked = await self._page.evaluate(
                """() => {
                    const btns = Array.from(
                        document.querySelectorAll("div[role='button']")
                    ).filter(b => {
                        const p = b.querySelector("path");
                        const d = p && p.getAttribute('d');
                        return d && d.startsWith('M7.92136');
                    });
                    if (!btns.length) return false;
                    btns[btns.length - 1].click();
                    return true;
                }"""
            )
            if not clicked:
                logger.warning(
                    "Не удалось найти кнопку 'Повторить' (regenerate) в интерфейсе."
                )
            return await self._wait_response_complete(
                wait_new=False, baseline_text=baseline_text, require_change=False
            )

    async def regenerate_stream(self, chat_id: Optional[str] = None):
        """Генератор: перегенерирует последний ответ и выдаёт (kind, delta)."""
        async with self._lock:
            if chat_id:
                await self.ensure_chat(chat_id)
            await self.ensure_logged_in()
            baseline_text = ""
            try:
                baseline_text = await self._extract_response_text()
            except Exception:
                pass
            clicked = await self._page.evaluate(
                """() => {
                    const btns = Array.from(
                        document.querySelectorAll("div[role='button']")
                    ).filter(b => {
                        const p = b.querySelector("path");
                        const d = p && p.getAttribute('d');
                        return d && d.startsWith('M7.92136');
                    });
                    if (!btns.length) return false;
                    btns[btns.length - 1].click();
                    return true;
                }"""
            )
            if not clicked:
                logger.warning(
                    "Не удалось найти кнопку 'Повторить' (regenerate) в интерфейсе."
                )
            async for kind, delta in self._stream_response(
                baseline_content=baseline_text, require_change=False
            ):
                yield kind, delta

    async def edit_message(self, index: int, new_content: str, chat_id: Optional[str] = None) -> str:
        async with self._lock:
            if chat_id:
                await self.ensure_chat(chat_id)
            await self.ensure_logged_in()

            messages = await self._user_message_elements()
            if index < 0 or index >= len(messages):
                raise BrowserSessionError(f"Сообщение с индексом {index} не найдено в интерфейсе.")

            target = messages[index]
            edit_button = await target.query_selector(settings.SEL_MESSAGE_EDIT_BUTTON)
            if edit_button is None:
                raise BrowserSessionError("Кнопка редактирования не найдена для указанного сообщения.")
            await edit_button.click()

            edit_input = await self._page.wait_for_selector(
                settings.SEL_MESSAGE_EDIT_INPUT, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
            )
            await edit_input.fill(new_content)

            await self._page.click(
                settings.SEL_MESSAGE_EDIT_SAVE, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
            )

            return await self._wait_response_complete(wait_new=False)

    async def edit_stream(self, index: int, new_content: str, chat_id: Optional[str] = None):
        """Генератор: редактирует сообщение пользователя и выдаёт (kind, delta)."""
        async with self._lock:
            if chat_id:
                await self.ensure_chat(chat_id)
            await self.ensure_logged_in()
            baseline_text = ""
            try:
                baseline_text = await self._extract_response_text()
            except Exception:
                pass

            messages = await self._user_message_elements()
            if index < 0 or index >= len(messages):
                raise BrowserSessionError(f"Сообщение с индексом {index} не найдено в интерфейсе.")

            target = messages[index]
            edit_button = await target.query_selector(settings.SEL_MESSAGE_EDIT_BUTTON)
            if edit_button is None:
                raise BrowserSessionError("Кнопка редактирования не найдена для указанного сообщения.")
            await edit_button.click()

            edit_input = await self._page.wait_for_selector(
                settings.SEL_MESSAGE_EDIT_INPUT, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
            )
            await edit_input.fill(new_content)

            await self._page.click(
                settings.SEL_MESSAGE_EDIT_SAVE, timeout=settings.get_timeout(settings.ACTION_TIMEOUT_MS)
            )

            async for kind, delta in self._stream_response(
                baseline_content=baseline_text, require_change=False
            ):
                yield kind, delta

    # ---------- Управление чатами / история / системный промпт / стоп ----------

    async def get_current_chat_id(self) -> Optional[str]:
        try:
            url = self._page.url
            m = re.search(r"/a/chat/s/([0-9a-fA-F-]{8,})", url)
            return m.group(1) if m else None
        except Exception:
            return None

    async def ensure_chat(self, chat_id: str) -> None:
        try:
            cur = await self.get_current_chat_id()
            if cur == chat_id:
                return
        except Exception:
            pass
        await self.switch_chat(chat_id)

    async def switch_chat(self, chat_id: str) -> None:
        url = f"{settings.CHAT_URL.rstrip('/')}/a/chat/s/{chat_id}"
        await self._page.goto(url, timeout=settings.get_timeout(settings.NAV_TIMEOUT_MS))
        await self._wait_navigation_settled()
        logger.info("Переключение на чат %s", chat_id)

    async def list_chats(self) -> List[dict]:
        try:
            return await self._page.evaluate(
                """() => {
                    const links = Array.from(
                        document.querySelectorAll("a[href*='/a/chat/s/']")
                    );
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\\/a\\/chat\\/s\\/([0-9a-fA-F\\-]+)/);
                        if (!m) continue;
                        const id = m[1];
                        if (seen.has(id)) continue;
                        seen.add(id);
                        out.push({
                            id,
                            title: (a.innerText || '').trim().slice(0, 80),
                            url: location.origin + '/a/chat/s/' + id,
                        });
                    }
                    return out;
                }"""
            )
        except Exception as exc:
            logger.warning("Не удалось получить список чатов: %s", exc)
            return []

    async def get_history(self) -> List[dict]:
        try:
            return await self._page.evaluate(
                """() => {
                    const items = Array.from(
                        document.querySelectorAll(
                            "div[class*='ds-message']:not([class*='main-content'])"
                        )
                    );
                    const out = [];
                    for (const it of items) {
                        const isAssistant = !!it.querySelector(
                            "[class*='ds-assistant-message-main-content']"
                        );
                        const role = isAssistant ? 'assistant' : 'user';
                        const think = it.querySelector("[class*='think']");
                        const reasoning = think ? think.innerText.trim() : "";
                        const clone = it.cloneNode(true);
                        const t = clone.querySelector("[class*='think']");
                        if (t) t.remove();
                        const content = (clone.innerText || "").trim();
                        out.push({ role, content, reasoning });
                    }
                    return out;
                }"""
            )
        except Exception as exc:
            logger.warning("Не удалось прочитать историю: %s", exc)
            return []

    async def _user_message_elements(self) -> List[Any]:
        """Возвращает DOM-элементы сообщений пользователя (обёртки ds-message
        без вложенного блока ассистента)."""
        wrappers = await self._page.query_selector_all(settings.SEL_MESSAGE_ITEM)
        user_msgs = []
        for w in wrappers:
            has_assistant = await w.query_selector(
                "[class*='ds-assistant-message-main-content']"
            )
            if not has_assistant:
                user_msgs.append(w)
        return user_msgs

    async def stop_generation(self) -> str:
        selectors = [
            "button[aria-label*='stop' i]",
            "button[aria-label*='Stop' i]",
            "button[aria-label*='остановить' i]",
            "[class*='stop']",
        ]
        for sel in selectors:
            try:
                btn = await self._page.wait_for_selector(
                    sel, timeout=1500, state="visible"
                )
                if btn:
                    await btn.click()
                    break
            except Exception:
                pass
        await asyncio.sleep(0.5)
        _, content = await self._extract_response_parts()
        return content

    async def extract_token_counts(self) -> Optional[tuple]:
        try:
            text = await self._page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('div,span,li,p'));
                    for (const e of els) {
                        const t = (e.innerText || '').trim();
                        if (/\\d+\\s*\\/\\s*\\d+/.test(t)) return t;
                    }
                    return '';
                }"""
            )
            if text:
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return None

    async def extract_citations(self) -> List[str]:
        """Возвращает список ссылок-источников web-поиска из последнего ответа.
        DeepSeek показывает источники в виде внешних ссылок внутри блока
        ответа ассистента (исключаем внутренние ссылки deepseek.com)."""
        try:
            return await self._page.evaluate(
                """() => {
                    const wrappers = Array.from(
                        document.querySelectorAll(
                            "div[class*='ds-message']:not([class*='main-content'])"
                        )
                    );
                    let last = null;
                    for (const w of wrappers) {
                        if (w.querySelector("[class*='ds-assistant-message-main-content']")) {
                            last = w;
                        }
                    }
                    if (!last) return [];
                    const out = [];
                    const seen = new Set();
                    const links = last.querySelectorAll("a[href]");
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        if (!href.startsWith('http')) continue;
                        if (/deepseek\\.com/i.test(href)) continue;
                        if (seen.has(href)) continue;
                        seen.add(href);
                        out.push(href);
                    }
                    return out;
                }"""
            ) or []
        except Exception:
            return []


browser_session = BrowserSession()
