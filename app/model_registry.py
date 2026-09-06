"""
Реестр режимов («моделей») моста — ЕДИНСТВЕННЫЙ источник правды о режимах.

Как добавить режим — см. docs/ADD_MODEL.md (чек-лист, искать по коду не нужно).
"""

from typing import Dict, List, Optional, Tuple

# name -> (deep_think, search)
DEFAULT_TOGGLES: Dict[str, Tuple[bool, bool]] = {
    "deepseek-chat": (False, False),
    "deepseek-think": (True, False),
    "deepseek-search": (False, True),
    "deepseek-think-search": (True, True),
}

DISPLAY_NAMES: Dict[str, str] = {
    "deepseek-chat": "DeepSeek Chat",
    "deepseek-think": "DeepSeek Think",
    "deepseek-search": "DeepSeek Search",
    "deepseek-think-search": "DeepSeek Think + Search",
}

DESCRIPTIONS: Dict[str, str] = {
    "deepseek-chat": "Обычный режим (без DeepThink и без поиска).",
    "deepseek-think": "DeepThink вкл, поиск выкл (аналог Reasoner).",
    "deepseek-search": "Web-поиск вкл, DeepThink выкл.",
    "deepseek-think-search": "DeepThink + Web-поиск одновременно.",
}

DEFAULT_MODEL = "deepseek-chat"


def resolve_toggles(
    name: str, overrides: Optional[Dict[str, Tuple[bool, bool]]] = None
) -> Tuple[bool, bool]:
    """Флаги (deep_think, search) для имени режима. Неизвестное имя -> дефолт."""
    key = (name or "").strip().lower()
    if overrides and key in overrides:
        return overrides[key]
    return DEFAULT_TOGGLES.get(key, DEFAULT_TOGGLES[DEFAULT_MODEL])


def list_models(
    overrides: Optional[Dict[str, Tuple[bool, bool]]] = None,
) -> List[dict]:
    """Список режимов для /v1/models. Env-оверрайды (MODE_TOGGLES) имеют приоритет."""
    names = list(overrides.keys()) if overrides else list(DEFAULT_TOGGLES.keys())
    return [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "deepseek-bridge",
            "display_name": DISPLAY_NAMES.get(name, name),
            "description": DESCRIPTIONS.get(name, "Режим DeepSeek."),
        }
        for name in names
    ]


def is_supported_model(
    name: str, overrides: Optional[Dict[str, Tuple[bool, bool]]] = None
) -> bool:
    key = (name or "").strip().lower()
    if overrides is not None:
        return key in overrides
    return key in DEFAULT_TOGGLES
