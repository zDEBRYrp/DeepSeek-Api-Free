"""
Микросервис-обёртка над веб-интерфейсом внутреннего тестового чат-приложения.
Предоставляет OpenAI-совместимый эндпоинт /v1/chat/completions.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.browser_session import BrowserSessionError, browser_session
from app.config import settings
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatSwitchRequest,
    Choice,
    ChoiceMessage,
    Tool,
    ToolCall,
    ToolCallFunction,
    Usage,
)
from app.search_client import search_web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(title="Internal Chat UI Bridge", version="1.0.0")

# Очередь запросов: браузер один, поэтому одновременно обрабатываем только
# один тяжёлый запрос (генерацию). Остальные получают 429 с небольшим
# "окном" ожидания, чтобы не блокировать клиента навсегда.
REQUEST_SEM = asyncio.Lock()
REQUEST_QUEUE_TIMEOUT = float(os.getenv("REQUEST_QUEUE_TIMEOUT", "2.0"))


async def _acquire_or_429() -> None:
    """Захватывает слот выполнения или возвращает 429, если сервис занят."""
    try:
        await asyncio.wait_for(REQUEST_SEM.acquire(), timeout=REQUEST_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail="Сервис занят обработкой другого запроса. "
                   "Одновременно поддерживается только один запрос генерации.",
            headers={"Retry-After": "5"},
        )


def _estimate_tokens(text: str) -> int:
    # Приближённая оценка (реальный токенайзер UI-модели недоступен)
    return max(1, len(text) // 4)


def _extract_last_user_message(messages: List[ChatMessage]) -> ChatMessage:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg
    raise HTTPException(status_code=400, detail="В запросе отсутствует сообщение пользователя.")


def _last_significant_message(messages: List[ChatMessage]) -> Optional[ChatMessage]:
    """Последнее осмысленное сообщение для отправки в DeepSeek: user или tool
    (результат инструмента тоже отправляем как сообщение)."""
    for msg in reversed(messages):
        if msg.role in ("user", "tool"):
            return msg
    return None


def _flatten_conversation(messages: List[ChatMessage]) -> str:
    """В режиме MEMORY_MODE=client вся история «сплющивается» в один промпт."""
    parts: List[str] = []
    for m in messages:
        if m.role == "system":
            continue
        text = m.content if isinstance(m.content, str) else ""
        if m.role == "user":
            parts.append(f"[user]\n{text}")
        elif m.role == "assistant":
            if m.tool_calls:
                for tc in m.tool_calls:
                    parts.append(f"[assistant -> вызов инструмента {tc.function.name}]\n{tc.function.arguments}")
            else:
                parts.append(f"[assistant]\n{text}")
        elif m.role == "tool":
            parts.append(f"[результат инструмента]\n{text}")
    return "\n\n".join(parts)


def _build_tool_instruction(tools: Optional[List[Tool]]) -> str:
    """Инструкция для DeepSeek, как оформлять вызов инструментов."""
    if not tools:
        return ""
    lines = [
        "Тебе доступны инструменты (function calling). Когда нужно вызвать инструмент, "
        "выведи ТОЛЬКО ОДНУ СТРОКУ чистого JSON без оформления — НЕ используй markdown, "
        "НЕ оборачивай в ```json заборы, НЕ ставь переносы строк внутри JSON:",
        '{"name": "<точное_имя_инструмента>", "arguments": { ... }}',
        "Имя инструмента должно быть ТОЧНО из списка ниже. Поле arguments — объект с "
        "параметрами по схеме инструмента. Когда получишь результат инструмента, "
        "продолжи помогать пользователю.",
        "ВАЖНО: в arguments передавай ТОЛЬКО те поля, что перечислены в схеме "
        "параметров инструмента. НЕ выдумывай и не добавляй свои поля "
        "(workdir, cwd, path, directory и т.п.) — клиент их не понимает и вызов "
        "инструмента завершится с ошибкой. Рабочую директорию задаёт сам клиент.",
        "Если для выполнения нужна папка, включи путь прямо в строку команды "
        "(например, 'cd \"C:\\Путь\" ; Get-ChildItem').",
        "ВАЖНО: если пользователь просит писать команды в markdown-заборах (```), "
        "всё равно используй формат вызова инструмента выше — клиент сам отобразит и "
        "выполнит команду, а просто написанный текст НЕ выполняется.",
        "Доступные инструменты:",
    ]
    for t in tools:
        f = t.function
        params = f.get("parameters", {})
        lines.append(
            f"- {f.get('name')}: {f.get('description', '')}  "
            f"схема аргументов: {json.dumps(params, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _safe_json_loads(s: str):
    """json.loads с ремонтом: DeepSeek иногда вставляет внутрь JSON служебные
    символы — закрывающие ``` заборы и реальные переносы строк внутри строковых
    значений (это невалидный JSON). Убираем заборы, заменяем переносы пробелами
    и чистим пробелы между ключом и двоеточием (иначе ключ "command " не совпадёт
    со схемой инструмента)."""
    try:
        return json.loads(s)
    except Exception:
        pass
    t = s.replace("```json", "").replace("```", "").replace("<tool_call>", "").replace("</tool_call>", "")
    t = t.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    t = re.sub(r'"([^"]*?)\s*"\s*:', r'"\1":', t)
    try:
        return json.loads(t)
    except Exception:
        return None


def _extract_json_objects(text: str):
    """Возвращает список (start, end, dict) для всех сбалансированных JSON-объектов
    верхнего уровня в тексте (с учётом вложенности и строк)."""
    objs = []
    start = None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                obj = _safe_json_loads(text[start:i + 1])
                if isinstance(obj, dict):
                    objs.append((start, i + 1, obj))
                start = None
    return objs


_SHELL_LANGS = {"", "bash", "sh", "shell", "zsh", "powershell", "pwsh", "ps", "cmd", "batch", "dos"}
_CMD_KEYWORDS = {
    "glob", "ls", "dir", "gci", "get-childitem", "echo", "cat", "type", "cd", "pwd",
    "git", "npm", "python", "py", "node", "ping", "curl", "irm", "invoke-webrequest",
    "cmd", "powershell", "get-location", "get-childitem", "get-content", "set-location",
    "get-item", "resolve-path", "find", "tree", "where",
}


def _looks_like_command(body: str) -> bool:
    first = body.split(None, 1)[0].lower() if body else ""
    return first in _CMD_KEYWORDS


def _cmd_to_toolcall(body: str, tool_names: set):
    """Превращает текстовую команду (из ```-блока или псевдокоманду) в вызов
    инструмента. `glob ...` маппится в glob-инструмент, остальное — в bash."""
    if "glob" in tool_names and re.match(r"glob\b", body, re.IGNORECASE):
        mg = re.match(r'glob\s+(?:-[A-Za-z]+\s+)?["\']?([^"\'\n]+?)["\']?\s*$', body, re.IGNORECASE)
        if mg:
            return {"name": "glob", "arguments": {"pattern": mg.group(1).strip()}}
    if "bash" in tool_names:
        return {"name": "bash", "arguments": {"command": body}}
    return None


def _canonical_call(call: dict) -> tuple:
    """Каноническое представление вызова для сравнения (ловим перевызовы)."""
    try:
        args = call.get("arguments") or {}
        if isinstance(args, str):
            args = _safe_json_loads(args) or {}
        s = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = ""
    return (call.get("name"), s)


def _executed_calls(messages) -> set:
    """Множество уже исполненных вызовов: assistant.tool_calls, по которым есть
    сообщение-результат (role='tool'). Нужно, чтобы разорвать цикл перевызовов."""
    has_result = any(getattr(m, "role", None) == "tool" for m in messages)
    if not has_result:
        return set()
    executed = set()
    for m in messages:
        if getattr(m, "role", None) == "assistant" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                f = tc.function
                executed.add(_canonical_call({"name": f.name, "arguments": f.arguments}))
    return executed


def _last_tool_result(messages) -> Optional[str]:
    for m in reversed(messages):
        if getattr(m, "role", None) == "tool":
            c = m.content
            return c if isinstance(c, str) else ""
    return None


def _prune_loop_calls(parsed: list, messages) -> tuple:
    """Отсеивает вызовы, уже исполненные в этой же беседе (цикл перевызовов).
    Возвращает (отфильтрованный_список, флаг_что_все_были_циклом)."""
    executed = _executed_calls(messages)
    if not executed:
        return parsed, False
    filtered = [c for c in parsed if _canonical_call(c) not in executed]
    return filtered, (bool(parsed) and not filtered)


def _parse_tool_call(text: str, tools: Optional[List[Tool]]):
    """Ищет в ответе DeepSeek вызовы инструмента. Возвращает
    (list[dict{name, arguments(dict)}], очищенный_текст). Пустой список = нет вызовов.

    Может вернуть несколько вызовов (параллельные tool_calls). Устойчив к формату:
    fenced ```json, <tool_call>, голый JSON в тексте, вложенные скобки в arguments.
    Имя инструмента НЕ фильтруется жёстко — клиент сам решает, есть ли у него такой инструмент.

    FALLBACK: DeepSeek иногда «ломает» fenced-JSON, вставляя закрывающий ``` прямо
    посреди объекта (напр. {"name": "bash", " \n ```arguments": {"command": ...}}). Тогда
    цельный JSON не парсится — вытаскиваем name и arguments по отдельности регэкспами.
    """
    if not tools:
        return [], text

    def _as_args(obj):
        args = obj.get("arguments", {})
        if isinstance(args, str):
            parsed = _safe_json_loads(args)
            args = parsed if isinstance(parsed, dict) else {}
        return args if isinstance(args, dict) else {}

    candidates = []  # (start, end, raw_or_dict)
    for m in re.finditer(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append((m.start(1), m.end(1), m.group(1)))
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        candidates.append((m.start(1), m.end(1), m.group(1)))
    for s, e, obj in _extract_json_objects(text):
        candidates.append((s, e, obj))

    found = []  # (start, end, name, args)
    for s, e, raw in candidates:
        obj = raw if isinstance(raw, dict) else None
        if obj is None:
            obj = _safe_json_loads(raw)
            if obj is None:
                continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or (obj.get("function") or {}).get("name")
        args = _as_args(obj)
        if name and isinstance(args, dict):
            found.append((s, e, name, args))

    # Убираем дубли: один и тот же объект может попасть и через явный
    # regex (fenced/<tool_call>), и через общий поиск _extract_json_objects.
    seen = set()
    unique = []
    for s, e, n, a in found:
        if (s, e) in seen:
            continue
        seen.add((s, e))
        unique.append((s, e, n, a))
    found = unique

    if found:
        stripped = text
        for s, e, _, _ in sorted(found, key=lambda x: x[0], reverse=True):
            stripped = stripped[:s] + stripped[e:]
        stripped = (
            stripped.replace("```json", "")
            .replace("```", "")
            .replace("<tool_call>", "")
            .replace("</tool_call>", "")
            .strip()
        )
        calls = [{"name": n, "arguments": a} for _, _, n, a in found]
        return calls, stripped

    # FALLBACK: сломанный fenced-JSON — name и arguments по отдельности.
    m_name = re.search(r'"name"\s*:\s*"([^"]*)"', text)
    if m_name:
        name = m_name.group(1)
        m_arg = re.search(r'arguments\s*"?\s*:\s*', text)
        if m_arg:
            rest = text[m_arg.end():].lstrip()
            args = None
            if rest.startswith("{"):
                objs = _extract_json_objects(rest)
                if objs and isinstance(objs[0][2], dict):
                    args = objs[0][2]
            elif rest.startswith('"'):
                m_str = re.match(r'("(?:\\.|[^"\\])*")', rest)
                if m_str:
                    args = _safe_json_loads(m_str.group(1))
                    if isinstance(args, str):
                        args = _safe_json_loads(args)
            if isinstance(args, dict):
                return [{"name": name, "arguments": args}], text

    # FALLBACK 2: пользователь просит «писать команды через ```» — DeepSeek выдаёт
    # их как текстовый code-блок (напр. `glob -p "**/*"`), а не как tool_call.
    # Чтобы клиент реально выполнил команду, превращаем такой блок в вызов
    # инструмента (glob/bash). Только для ```-блоков — голый текст не трогаем,
    # иначе развёрнутые описания вроде «glob pattern * - path C:\...» превращаются
    # в мусорные вызовы и уводят клиента в бесконечный цикл перевызовов.
    tool_names = {t.get("function", {}).get("name") for t in tools}
    if tool_names & {"bash", "glob"}:
        m_block = re.search(r"```(\w*)\n(.*?)\n```", text, re.DOTALL)
        if not m_block:
            m_block = re.search(r"```(\w*)\s*(.*?)\s*```", text, re.DOTALL)
        if m_block:
            lang = m_block.group(1).lower()
            body = m_block.group(2).strip()
            if body and not body.lstrip().startswith("{"):
                if lang in _SHELL_LANGS or _looks_like_command(body):
                    call = _cmd_to_toolcall(body, tool_names)
                    if call:
                        stripped = (text[:m_block.start()] + text[m_block.end():]).strip()
                        return [call], stripped
    return [], text


def _save_images_to_tmp(message: ChatMessage) -> List[str]:
    """Сохраняет base64-изображения во временные файлы для последующей загрузки в UI."""
    import base64

    paths: List[str] = []
    if not message.images:
        return paths

    tmp_dir = Path(tempfile.mkdtemp(prefix="chat_upload_"))
    for i, image in enumerate(message.images):
        if image.type == "image_path":
            paths.append(image.value)
        elif image.type == "image_base64":
            file_path = tmp_dir / f"image_{i}.png"
            file_path.write_bytes(base64.b64decode(image.value))
            paths.append(str(file_path))
        elif image.type == "image_url":
            # Загрузка по URL сознательно не выполняется здесь — рекомендуется
            # заранее скачать файл на стороне клиента и передать image_path/base64.
            raise HTTPException(
                status_code=400,
                detail="image_url не поддерживается напрямую, используйте image_base64 или image_path.",
            )
    return paths


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Запуск браузерной сессии...")
    await browser_session.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await browser_session.close()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/debug/dump")
async def debug_dump():
    """Отладочный эндпоинт: сбрасывает HTML текущей страницы в debug_page.html."""
    url = await browser_session.dump_page_html()
    return {"ok": True, "url": url}


@app.get("/debug/dom")
async def debug_dom():
    """Отладочный эндпоинт: возвращает структуру DOM (сообщения/поле ввода/кнопки)."""
    return await browser_session.introspect_dom()


@app.post("/v1/chat/new")
async def new_chat():
    """Создаёт новый чат в интерфейсе DeepSeek и возвращает его id/URL."""
    try:
        url = await browser_session.new_chat()
        chat_id = await browser_session.get_current_chat_id()
        return {"ok": True, "chat_id": chat_id, "url": url}
    except BrowserSessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/chat/list")
async def list_chats():
    """Возвращает список чатов из боковой панели DeepSeek."""
    try:
        chats = await browser_session.list_chats()
        return {"chats": chats}
    except BrowserSessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/chat/switch")
async def switch_chat(req: ChatSwitchRequest):
    """Переключается на чат с заданным conversation_id."""
    try:
        await browser_session.switch_chat(req.chat_id)
        return {"ok": True, "chat_id": req.chat_id}
    except BrowserSessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/chat/history")
async def chat_history():
    """Возвращает всю переписку текущего чата (role/content/reasoning)."""
    try:
        messages = await browser_session.get_history()
        return {"messages": messages}
    except BrowserSessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/chat/stop")
async def stop_generation():
    """Прерывает текущую генерацию и возвращает то, что успело нагенериться."""
    try:
        content = await browser_session.stop_generation()
        return {"ok": True, "content": content}
    except BrowserSessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/models")
async def list_models():
    """Список доступных «моделей» - режимов (think/search) обёртки.
    Формируется из MODE_TOGGLES (единственный источник правды)."""
    owned = "deepseek-chat"
    descriptions = {
        (False, False): "Обычный режим (без DeepThink и без поиска).",
        (True, False): "DeepThink вкл, поиск выкл (аналог Reasoner).",
        (False, True): "Web-поиск вкл, DeepThink выкл.",
        (True, True): "DeepThink + Web-поиск одновременно.",
    }
    data = [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": owned,
            "description": descriptions.get(flags, "Режим DeepSeek."),
        }
        for name, flags in settings.MODE_TOGGLES.items()
    ]
    return {"object": "list", "data": data}


@app.get("/debug/extract")
async def debug_extract():
    """Отладка: что возвращает извлечение ответа из текущей страницы."""
    if not browser_session._page:
        return {"error": "no page"}
    count = len(await browser_session._page.query_selector_all(settings.SEL_ASSISTANT_BLOCK))
    text = await browser_session._extract_response_text()
    return {"count": count, "text_len": len(text), "text": text[:500], "url": browser_session._page.url}


@app.get("/debug/type_test")
async def debug_type_test():
    """Отладка: набираем текст в textarea и читаем обратно input_value."""
    if not browser_session._page:
        return {"error": "no page"}
    try:
        box = await browser_session._page.wait_for_selector(
            "textarea", state="attached", timeout=5000
        )
        await box.click()
        await box.type("test123", timeout=5000)
        val = await box.input_value()
        return {"typed_value": val, "url": browser_session._page.url}
    except Exception as exc:
        return {"error": str(exc)}


def _sse_chunk(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# Подсказка при авто-повторе, если модель вернула пустой ответ: заставляем
# её выдать итоговый текст и не плодить лишние вызовы инструментов.
EMPTY_ANSWER_NUDGE = (
    "Ответь обязательно текстом, кратко и по существу. "
    "Не вызывай инструменты повторно без крайней необходимости."
)
# Что вернуть клиенту, если после всех повторов ответ всё равно пустой
# (чтобы клиент не зависал и не терял ход).
EMPTY_ANSWER_FALLBACK = (
    "⚠️ Модель вернула пустой ответ. Попробуйте переформулировать запрос "
    "или повторите его через несколько секунд."
)
MAX_EMPTY_RETRIES = 2


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    # Обычная отправка требует поле messages.
    if not request.regenerate and request.edit is None and not request.messages:
        raise HTTPException(
            status_code=400, detail="Поле 'messages' обязательно для отправки сообщения."
        )

    # Захват слота очереди (или 429, если сервис уже занят генерацией).
    await _acquire_or_429()

    # Маппинг model -> режим (Deepseek-Think / Search / Think_Search).
    # Если model распознан - он имеет приоритет над флагами deep_think/search.
    eff_deep_think = request.deep_think
    eff_search = request.search
    model_key = (request.model or "").strip().lower()
    if model_key in settings.MODE_TOGGLES:
        eff_deep_think, eff_search = settings.MODE_TOGGLES[model_key]

    # Определяем источник приращений (kind, delta) в зависимости от режима запроса.
    # kind in {'reasoning','content'} - рассуждения DeepThink и итоговый ответ.
    async def _source(nudge: bool = False):
        # При повторе (nudge) докидываем подсказку «ответь текстом» в конец истории,
        # чтобы модель не возвращала пустой ответ и не вызывала инструменты зря.
        msgs = request.messages
        if nudge and msgs:
            msgs = list(msgs) + [ChatMessage(role="user", content=EMPTY_ANSWER_NUDGE)]
        # --- Регенерация последнего ответа ---
        if request.regenerate:
            async for kind, delta in browser_session.regenerate_stream(
                chat_id=request.conversation_id
            ):
                yield kind, delta

        # --- Редактирование сообщения из истории с пересчётом ---
        elif request.edit is not None:
            async for kind, delta in browser_session.edit_stream(
                request.edit.index, request.edit.content,
                chat_id=request.conversation_id,
            ):
                yield kind, delta

        # --- Обычная отправка нового сообщения ---
        else:
            tools = request.tools
            system_msgs = [m for m in msgs if m.role == "system"]
            system_text = system_msgs[-1].content if system_msgs else ""
            last_user = _extract_last_user_message(msgs)
            mode = settings.MEMORY_MODE

            if mode == "client":
                # Клиент сам шлёт всю историю: сплющиваем её в ОДИН промпт и
                # каждый раз начинаем НОВЫЙ чат DeepSeek (полностью stateless,
                # без утечки прошлых ответов). Так работают opencode/Kilo Code.
                conv = _flatten_conversation(msgs)
                tool_instr = _build_tool_instruction(tools) if tools else ""
                sys_part = system_text
                if tool_instr:
                    sys_part = (sys_part + "\n\n" + tool_instr).strip() if sys_part else tool_instr
                prompt = settings.SYSTEM_PROMPT_TEMPLATE.format(system=sys_part, user=conv) if sys_part else conv
                file_paths = _save_images_to_tmp(last_user)
                # В client-режиме ВСЕГДА новый чат (каждый запрос независим).
                await browser_session.new_chat()
                async for kind, delta in browser_session.stream_message(
                    prompt,
                    file_paths,
                    deep_think=eff_deep_think,
                    search=eff_search,
                    chat_id=None,
                ):
                    yield kind, delta
            else:
                # server-режим: отправляем последнее сообщение (user или
                # результат инструмента) в существующий чат-тред DeepSeek.
                last = _last_significant_message(msgs)
                tool_instr = _build_tool_instruction(tools) if tools else ""
                if last and last.role == "tool":
                    prompt = "[результат вызова инструмента]\n" + (last.content if isinstance(last.content, str) else "")
                    if tool_instr:
                        prompt = settings.SYSTEM_PROMPT_TEMPLATE.format(system=tool_instr, user=prompt)
                else:
                    prompt = last_user.content if isinstance(last_user.content, str) else ""
                    sys_part = system_text
                    if tool_instr:
                        sys_part = (sys_part + "\n\n" + tool_instr).strip() if sys_part else tool_instr
                    if sys_part:
                        prompt = settings.SYSTEM_PROMPT_TEMPLATE.format(system=sys_part, user=prompt)
                file_paths = _save_images_to_tmp(last_user)
                if request.new_chat:
                    await browser_session.new_chat()
                async for kind, delta in browser_session.stream_message(
                    prompt,
                    file_paths,
                    deep_think=eff_deep_think,
                    search=eff_search,
                    chat_id=request.conversation_id,
                ):
                    yield kind, delta

    async def _finalize_usage(prompt_tokens: int, completion_tokens: int):
        ds_counter = None
        try:
            tc = await browser_session.extract_token_counts()
            if tc:
                ds_counter = f"{tc[0]}/{tc[1]}"
                # Реальный счётчик UI (если есть) уточняет общее число токенов.
                return tc[0], ds_counter
        except Exception:
            pass
        return prompt_tokens + completion_tokens, ds_counter

    # --- Потоковый режим (SSE) ---
    if request.stream:
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        prompt_tokens = sum(_estimate_tokens(m.content) for m in (request.messages or []))

        async def event_generator():
            try:
                # Первый чанк: роль ассистента, без контента.
                yield _sse_chunk({
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                })

                # Генерация с авто-повтором: если модель вернула ПУСТОЙ ответ
                # (без текста и без вызова инструмента), повторяем с подсказкой
                # «ответь текстом», чтобы клиент не зависал и получал итог.
                final_text = ""
                final_tool_calls = []
                completion_tokens = 0
                reasoning_tokens = 0
                attempt = 0
                while True:
                    acc_text = ""
                    acc_reason = ""
                    comp_tok = 0
                    # При наличии tools не стримим по кусочкам, а копим полный ответ,
                    # чтобы корректно отдать tool_calls в финальном чанке.
                    stream_incremental = not request.tools
                    try:
                        async for kind, delta in _source(nudge=(attempt > 0)):
                            if kind == "reasoning":
                                reasoning_tokens += _estimate_tokens(delta)
                                acc_reason += delta
                                if stream_incremental:
                                    yield _sse_chunk({
                                        "id": chat_id, "object": "chat.completion.chunk",
                                        "created": created, "model": request.model,
                                        "choices": [{"index": 0, "delta": {"reasoning_content": delta}, "finish_reason": None}],
                                    })
                            else:
                                comp_tok += _estimate_tokens(delta)
                                acc_text += delta
                                if stream_incremental:
                                    yield _sse_chunk({
                                        "id": chat_id, "object": "chat.completion.chunk",
                                        "created": created, "model": request.model,
                                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                                    })
                    except BrowserSessionError as exc:
                        yield _sse_chunk({"error": {"message": str(exc), "type": "server_error"}})
                        return
                    except Exception as exc:
                        logger.exception("Ошибка потоковой генерации")
                        yield _sse_chunk({"error": {"message": f"Внутренняя ошибка: {exc}", "type": "server_error"}})
                        return

                    # Разбор вызовов инструмента (если клиент передал tools).
                    tool_calls = []
                    if request.tools and acc_text:
                        parsed, stripped = _parse_tool_call(acc_text, request.tools)
                        if parsed:
                            parsed, looped_all = _prune_loop_calls(parsed, request.messages)
                            if looped_all:
                                # Модель перевыпускает уже исполненный вызов — цикл.
                                # Разрываем его: отдаём последний результат инструмента
                                # как финальный текст, без tool_calls.
                                last_res = _last_tool_result(request.messages)
                                acc_text = last_res or "Результат выполнения команды получен (см. выше)."
                                tool_calls = []
                            else:
                                tool_calls = parsed
                                acc_text = stripped

                    if acc_text or tool_calls:
                        final_text, final_tool_calls = acc_text, tool_calls
                        completion_tokens = comp_tok
                        break
                    # Пустой ответ — повторяем с подсказкой, иначе заглушка.
                    attempt += 1
                    if attempt > MAX_EMPTY_RETRIES:
                        final_text = EMPTY_ANSWER_FALLBACK
                        break

                total_tokens, ds_counter = await _finalize_usage(
                    prompt_tokens, completion_tokens
                )
                conv_id = await browser_session.get_current_chat_id()
                citations = []
                if eff_search:
                    try:
                        citations = await browser_session.extract_citations()
                    except Exception:
                        citations = []

                if final_tool_calls:
                    # Чанк с вызовом инструмента (совместимо с OpenAI SDK):
                    # сначала delta с tool_calls (finish_reason=null), затем пустой
                    # чанк с finish_reason="tool_calls". Поддерживаем несколько
                    # параллельных вызовов (каждый со своим index/id).
                    tc_delta = [
                        {"index": i, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                         "function": {"name": tc["name"],
                                      "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                        for i, tc in enumerate(final_tool_calls)
                    ]
                    yield _sse_chunk({
                        "id": chat_id, "object": "chat.completion.chunk",
                        "created": created, "model": request.model,
                        "conversation_id": conv_id,
                        "choices": [{"index": 0, "delta": {"tool_calls": tc_delta}, "finish_reason": None}],
                    })
                    yield _sse_chunk({
                        "id": chat_id, "object": "chat.completion.chunk",
                        "created": created, "model": request.model,
                        "conversation_id": conv_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {
                            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens, "ds_token_counter": ds_counter,
                        },
                        **({"citations": citations} if citations else {}),
                    })
                else:
                    # Обычный текстовый ответ. При наличии tools мы не стримили
                    # по кусочкам (stream_incremental=False), поэтому отдаём
                    # накопленный final_text одним контент-чанком — иначе клиент
                    # (OpenCode/Kilo) вообще ничего не покажет.
                    if final_text:
                        yield _sse_chunk({
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.model,
                            "conversation_id": conv_id,
                            "choices": [{"index": 0, "delta": {"content": final_text}, "finish_reason": None}],
                        })
                    yield _sse_chunk({
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.model,
                        "conversation_id": conv_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens,
                            "ds_token_counter": ds_counter,
                        },
                        **({"citations": citations} if citations else {}),
                    })
                yield "data: [DONE]\n\n"
            finally:
                # Потоковый режим освобождает слот сам, по завершении генерации.
                REQUEST_SEM.release()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Обычный (непотоковый) режим ---
    try:
        try:
            response_text = ""
            reasoning_text = ""
            tool_calls_obj = None
            attempt = 0
            while True:
                rtext = ""
                rreason = ""
                async for kind, delta in _source(nudge=(attempt > 0)):
                    if kind == "reasoning":
                        rreason += delta
                    else:
                        rtext += delta
                # Разбор вызовов инструмента (если клиент передал tools).
                parsed = []
                if request.tools and rtext:
                    parsed, stripped = _parse_tool_call(rtext, request.tools)
                    if parsed:
                        parsed, looped_all = _prune_loop_calls(parsed, request.messages)
                        if looped_all:
                            # Модель перевыпускает уже исполненный вызов — цикл.
                            # Разрываем: отдаём последний результат как текст.
                            last_res = _last_tool_result(request.messages)
                            rtext = last_res or "Результат выполнения команды получен (см. выше)."
                            parsed = []
                        else:
                            rtext = stripped
                if rtext or parsed:
                    response_text = rtext
                    reasoning_text = rreason
                    if parsed:
                        tool_calls_obj = [
                            ToolCall(function=ToolCallFunction(
                                name=tc["name"],
                                arguments=json.dumps(tc["arguments"], ensure_ascii=False),
                            ))
                            for tc in parsed
                        ]
                    break
                # Пустой ответ — повторяем с подсказкой, иначе заглушка.
                attempt += 1
                if attempt > MAX_EMPTY_RETRIES:
                    response_text = EMPTY_ANSWER_FALLBACK
                    break

            if tool_calls_obj is None and browser_session._looks_busy(response_text):
                raise BrowserSessionError(
                    "DeepSeek временно занят (одновременно обрабатывается только один запрос). "
                    "Подождите несколько секунд и повторите запрос."
                )
        except BrowserSessionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # непредвиденная ошибка UI-автоматизации
            logger.exception("Ошиброобработки запроса")
            raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {exc}") from exc

        # (tool_calls_obj уже сформирован выше)

        prompt_tokens = sum(_estimate_tokens(m.content) for m in (request.messages or []))
        completion_tokens = _estimate_tokens(response_text)
        total_tokens, ds_counter = await _finalize_usage(prompt_tokens, completion_tokens)
        conv_id = await browser_session.get_current_chat_id()

        citations = []
        if eff_search:
            try:
                citations = await browser_session.extract_citations()
            except Exception:
                citations = []

        return ChatCompletionResponse(
            model=request.model,
            choices=[Choice(
                message=ChoiceMessage(
                    content=None if tool_calls_obj else (response_text if response_text else None),
                    reasoning_content=reasoning_text or None,
                    tool_calls=tool_calls_obj,
                ),
                finish_reason="tool_calls" if tool_calls_obj else "stop",
            )],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                ds_token_counter=ds_counter,
            ),
            conversation_id=conv_id,
            **({"citations": citations} if citations else {}),
        )
    finally:
        # Непотоковый режим освобождает слот здесь, после формирования ответа.
        REQUEST_SEM.release()


def _openai_error(status: int, message: str, etype: str) -> JSONResponse:
    # OpenAI-совместимый вид ошибки, чтобы зод-валидирующие клиенты
    # (AI SDK и др.) выдавали понятное сообщение, а не "Type validation failed".
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": etype}})


@app.exception_handler(BrowserSessionError)
async def browser_error_handler(_, exc: BrowserSessionError):
    return _openai_error(502, str(exc), "server_error")


@app.exception_handler(HTTPException)
async def http_error_handler(_, exc: HTTPException):
    etype = "invalid_request_error" if exc.status_code == 400 else "server_error"
    return _openai_error(exc.status_code, str(exc.detail), etype)
