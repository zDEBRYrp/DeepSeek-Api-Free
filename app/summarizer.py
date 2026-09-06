"""
Сжатие длинного диалога внешним LLM.

Провайдер настраивается (любой OpenAI-совместимый endpoint):
SUMMARIZER_API_URL / SUMMARIZER_API_KEY / SUMMARIZER_MODEL
(+ цепочка запасных моделей SUMMARIZER_MODEL_FALLBACKS, перебор до успеха).
"""

import logging
from typing import List, Optional

import httpx

logger = logging.getLogger("summarizer")


class SummarizerUnavailableError(Exception):
    """Суммаризатор не настроен или все модели цепочки отказали."""


SYSTEM_PROMPT = (
    "Ты — помощник для сжатия диалога. Сожми переданный фрагмент переписки "
    "(вопросы пользователя и ответы ассистента) в краткую выжимку на 3–7 предложений. "
    "Сохрани: тему, ключевые факты, принятые решения, открытые вопросы. "
    "Убери воду и повторы. Отвечай на том же языке, что и диалог. "
    "Верни ТОЛЬКО текст выжимки, без префиксов и пояснений."
)


async def _call_once(
    api_url: str, api_key: str, model: str, text: str, timeout_s: float
) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }
    timeout = httpx.Timeout(connect=5.0, read=timeout_s, write=timeout_s, pool=timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(api_url.rstrip("/") + "/chat/completions",
                                 headers=headers, json=payload)
    if resp.status_code != 200:
        raise SummarizerUnavailableError(
            f"Summarizer upstream returned status {resp.status_code}"
        )
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise SummarizerUnavailableError(f"Bad summarizer response: {exc}") from exc
    summary = str(content or "").strip()
    if not summary:
        raise SummarizerUnavailableError("Summarizer returned empty summary")
    return summary


async def summarize_text(
    text: str,
    *,
    api_url: str,
    api_key: str = "",
    model: str = "gpt-4o-mini",
    fallbacks: Optional[List[str]] = None,
    timeout_s: float = 30.0,
) -> str:
    """Сжать `text` в выжимку. Перебирает model + fallbacks до первого успеха."""
    if not api_url:
        raise SummarizerUnavailableError("SUMMARIZER_API_URL is empty")
    last_error: Exception | None = None
    for candidate in [model, *(fallbacks or [])]:
        try:
            return await _call_once(api_url, api_key, candidate, text, timeout_s)
        except Exception as exc:  # пробуем следующую модель цепочки
            last_error = exc
            logger.warning("Summarizer model '%s' failed: %s", candidate, exc)
            continue
    raise SummarizerUnavailableError(
        f"All summarizer models failed: {last_error}"
    ) from last_error
