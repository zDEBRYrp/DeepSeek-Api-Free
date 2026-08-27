"""
Простой клиент внешнего поискового API (произвольный REST-поисковик,
формат ответа нужно адаптировать под конкретного провайдера).
"""

import httpx

from app.config import settings


async def search_web(query: str) -> str:
    """
    Выполняет запрос к внешнему поисковому API и возвращает
    краткую текстовую выжимку результатов для подмешивания в промпт.
    При отсутствии настроенного провайдера возвращает пустую строку.
    """
    if not settings.SEARCH_API_URL:
        return ""

    headers = {}
    if settings.SEARCH_API_KEY:
        headers["Authorization"] = f"Bearer {settings.SEARCH_API_KEY}"

    params = {"q": query, "limit": settings.SEARCH_RESULTS_LIMIT}

    timeout = httpx.Timeout(settings.get_timeout(15000) / 1000)  # ~15 сек с погрешностью
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(settings.SEARCH_API_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # сеть/провайдер поиска нестабильны — не роняем основной запрос
            return f"[поиск временно недоступен: {exc}]"

    # Ожидаемый общий формат: {"results": [{"title": "...", "snippet": "..."}], ...}
    items = data.get("results", [])[: settings.SEARCH_RESULTS_LIMIT]
    if not items:
        return ""

    lines = ["Результаты внешнего поиска:"]
    for i, item in enumerate(items, start=1):
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        lines.append(f"{i}. {title} — {snippet}")
    return "\n".join(lines)
