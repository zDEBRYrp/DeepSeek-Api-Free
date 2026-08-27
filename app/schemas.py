"""
Схемы запросов/ответов, совместимые по форме с OpenAI Chat Completions API,
расширенные служебными полями deep_think / search / edit / regenerate.
"""

import time
import uuid
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


class ImagePart(BaseModel):
    # Мультимодальная часть сообщения — путь/URL к изображению или base64
    type: Literal["image_url", "image_path", "image_base64"] = "image_path"
    value: str


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str
    images: Optional[List[ImagePart]] = None


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
