"""
Схемы запросов/ответов, совместимые по форме с OpenAI Chat Completions API,
расширенные служебными полями deep_think / search / edit / regenerate.
"""

import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class ImagePart(BaseModel):
    # Мультимодальная часть сообщения — путь/URL к изображению или base64
    type: Literal["image_url", "image_path", "image_base64"] = "image_path"
    value: str


def _image_part_from_url(url: str) -> Dict[str, str]:
    """Конвертирует URL/Data-URI изображения в формат ImagePart."""
    if url.startswith("data:image"):
        # data:image/png;base64,xxxx -> забираем часть после запятой
        return {"type": "image_base64", "value": url.partition(",")[2]}
    if url.startswith(("http://", "https://")):
        return {"type": "image_url", "value": url}
    return {"type": "image_path", "value": url}


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    # OpenAI-совместимо: content может быть строкой ИЛИ списком content-частей
    # (multimodal: [{"type":"text","text":...}, {"type":"image_url","image_url":{...}}]).
    content: Union[str, List[Dict[str, Any]]] = ""
    images: Optional[List[ImagePart]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_content(cls, data):
        if not isinstance(data, dict):
            return data
        content = data.get("content")
        if isinstance(content, list):
            texts: List[str] = []
            imgs = list(data.get("images") or [])
            for part in content:
                if not isinstance(part, dict):
                    texts.append(str(part))
                    continue
                ptype = part.get("type", "text")
                if ptype == "text":
                    texts.append(str(part.get("text", "")))
                elif ptype == "image_url":
                    url = part.get("image_url", {})
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    if url:
                        imgs.append(_image_part_from_url(url))
                # input_audio и прочие типы игнорируем
            data = dict(data)
            data["content"] = "\n".join(t for t in texts if t)
            data["images"] = imgs
        return data


class EditInstruction(BaseModel):
    # Индекс редактируемого пользовательского сообщения (0-based среди role=="user")
    index: int
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "internal-chat-ui"
    messages: Optional[List[ChatMessage]] = None
    stream: bool = False
    temperature: Optional[float] = None  # не используется UI, оставлено для совместимости

    # Служебные расширения
    deep_think: bool = False
    search: bool = False
    new_chat: bool = False
    edit: Optional[EditInstruction] = None
    regenerate: bool = False
    conversation_id: Optional[str] = None


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str
    reasoning_content: Optional[str] = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    # По умолчанию - приближённая оценка; при наличии счётчика в UI
    # total_tokens может быть уточнён (см. extract_token_counts).
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    ds_token_counter: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[Choice]
    usage: Usage
    conversation_id: Optional[str] = None
    # Источники web-поиска (ссылки), если поиск был включён.
    citations: Optional[List[str]] = None


class ChatSwitchRequest(BaseModel):
    chat_id: str
