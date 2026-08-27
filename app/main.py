"""
Микросервис-обёртка над веб-интерфейсом внутреннего тестового чат-приложения.
Предоставляет OpenAI-совместимый эндпоинт /v1/chat/completions.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import List

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
    async def _source():
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
            last_message = _extract_last_user_message(request.messages)
            prompt = last_message.content

            # Системный промпт (если передан в messages, role=system) вшиваем
            # прямо в текст сообщения, т.к. UI DeepSeek не имеет отдельного поля.
            system_msgs = [m for m in request.messages if m.role == "system"]
            if system_msgs:
                prompt = settings.SYSTEM_PROMPT_TEMPLATE.format(
                    system=system_msgs[-1].content, user=prompt
                )

            file_paths = _save_images_to_tmp(last_message)
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
                completion_tokens = 0
                reasoning_tokens = 0
                try:
                    async for kind, delta in _source():
                        if kind == "reasoning":
                            reasoning_tokens += _estimate_tokens(delta)
                            field = "reasoning_content"
                        else:
                            completion_tokens += _estimate_tokens(delta)
                            field = "content"
                        yield _sse_chunk({
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.model,
                            "choices": [
                                {"index": 0, "delta": {field: delta}, "finish_reason": None}
                            ],
                        })
                except BrowserSessionError as exc:
                    yield _sse_chunk({"error": {"message": str(exc), "type": "server_error"}})
                    return
                except Exception as exc:
                    logger.exception("Ошибка потоковой генерации")
                    yield _sse_chunk({"error": {"message": f"Внутренняя ошибка: {exc}", "type": "server_error"}})
                    return

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
                # Финальный чанк с завершением.
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
            async for kind, delta in _source():
                if kind == "reasoning":
                    reasoning_text += delta
                else:
                    response_text += delta
            if browser_session._looks_busy(response_text):
                raise BrowserSessionError(
                    "DeepSeek временно занят (одновременно обрабатывается только один запрос). "
                    "Подождите несколько секунд и повторите запрос."
                )
        except BrowserSessionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # непредвиденная ошибка UI-автоматизации
            logger.exception("Ошибка обработки запроса")
            raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {exc}") from exc

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
            choices=[Choice(message=ChoiceMessage(
                content=response_text, reasoning_content=reasoning_text or None
            ))],
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
