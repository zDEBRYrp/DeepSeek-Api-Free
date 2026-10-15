import types
import unittest
import json
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main
from app.schemas import ChatCompletionRequest, ChatSwitchRequest
from tests.helpers import collect_async_iter, run_inline


CHAT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class FakeSession:
    def __init__(self):
        self.prepare_chat = AsyncMock(return_value=CHAT_A)
        self.list_chats = AsyncMock(return_value=[{"id": CHAT_A, "title": "A"}])
        self.switch_chat = AsyncMock()
        self.get_current_chat_id = AsyncMock(return_value=CHAT_A)
        self.ensure_chat = AsyncMock()
        self.get_history = AsyncMock(return_value=[{"role": "user", "content": "hi"}])
        self.stop_generation = AsyncMock(return_value="partial")
        self.extract_token_counts = AsyncMock(return_value=None)
        self.extract_citations = AsyncMock(return_value=[])
        self.stream_responses = ["answer"]

    def get_current_url(self):
        return f"https://chat.deepseek.com/a/chat/s/{CHAT_A}"

    def _looks_busy(self, text):
        return text == "сервис занят"

    async def stream_message(self, *args, **kwargs):
        self.last_stream_args = args
        self.last_stream_kwargs = kwargs
        yield "content", self.stream_responses.pop(0)


class FakePool:
    def __init__(self, session):
        self.session = session
        self.size = 1
        self.acquired_chat_ids = []
        self.bound = []
        self.released = []

    def __len__(self):
        return self.size

    async def acquire(self, timeout=None, chat_id=None):
        self.acquired_chat_ids.append(chat_id)
        return self.session

    async def bind_chat(self, session, chat_id):
        self.bound.append((session, chat_id))

    async def release(self, session):
        self.released.append(session)

    async def get_session_for_chat(self, chat_id):
        return self.session if chat_id in (None, CHAT_A) else None

    async def get_status_summary(self):
        return {"total": 1, "active": 1, "cooling": 0, "busy": 0, "profiles": ["fake"]}


class ManagementEndpointTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.pool = FakePool(self.session)
        self.pool_patch = patch.object(main, "session_pool", self.pool)
        self.pool_patch.start()

    def tearDown(self):
        self.pool_patch.stop()

    def test_health_models_and_chat_management(self):
        health = run_inline(main.healthz())
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["version"], "1.1.0")
        self.assertEqual(health["pool"]["total"], 1)
        self.assertGreaterEqual(len(run_inline(main.list_models())["data"]), 4)

        with patch.object(main.settings, "get_initial_message", return_value="boot"):
            created = run_inline(main.new_chat())
        self.assertEqual(created["chat_id"], CHAT_A)
        self.session.prepare_chat.assert_awaited_with(
            initial_message="boot",
            initial_deep_think=False,
            initial_search=False,
            create_new=True,
        )

        listed = run_inline(main.list_chats())
        self.assertEqual(listed["chats"][0]["id"], CHAT_A)

        switched = run_inline(
            main.switch_chat(ChatSwitchRequest(chat_id=CHAT_A))
        )
        self.assertTrue(switched["ok"])

        history = run_inline(main.chat_history(CHAT_A))
        self.assertEqual(history["chat_id"], CHAT_A)
        self.session.ensure_chat.assert_awaited_with(CHAT_A)

        stopped = run_inline(main.stop_generation(CHAT_A))
        self.assertEqual(stopped["content"], "partial")

        ui = run_inline(main.web_ui())
        self.assertEqual(ui.media_type, "text/html")

    def test_http_error_preserves_retry_after(self):
        response = run_inline(
            main.http_error_handler(
                None,
                HTTPException(
                    status_code=429,
                    detail="busy",
                    headers={"Retry-After": "7"},
                ),
            )
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "7")
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_multi_session_history_and_stop_require_chat_id(self):
        self.pool.size = 2
        with self.assertRaises(HTTPException) as history_error:
            run_inline(main.chat_history())
        self.assertEqual(history_error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as stop_error:
            run_inline(main.stop_generation())
        self.assertEqual(stop_error.exception.status_code, 400)

    def test_non_stream_completion_keeps_bootstrap_chat(self):
        request = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "hello"}],
        )
        http_request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1")
        )
        with (
            patch.object(main.settings, "DISABLE_RATE_LIMIT", True),
            patch.object(main.settings, "MEMORY_MODE", "server"),
            patch.object(main.settings, "get_initial_message", return_value="boot"),
        ):
            response = run_inline(main.chat_completions(request, http_request))

        self.assertEqual(response.conversation_id, CHAT_A)
        self.assertEqual(response.choices[0].message.content, "answer")
        self.session.prepare_chat.assert_awaited_once_with(
            initial_message="boot",
            initial_deep_think=False,
            initial_search=False,
            create_new=False,
            chat_id=None,
        )
        self.assertEqual(self.session.last_stream_kwargs["chat_id"], CHAT_A)
        self.assertEqual(self.pool.bound[-1], (self.session, CHAT_A))
        self.assertEqual(self.pool.released[-1], self.session)

    def test_stream_completion_emits_done_and_releases_session(self):
        request = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        http_request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1")
        )
        with (
            patch.object(main.settings, "DISABLE_RATE_LIMIT", True),
            patch.object(main.settings, "MEMORY_MODE", "server"),
            patch.object(main.settings, "get_initial_message", return_value=""),
        ):
            response = run_inline(main.chat_completions(request, http_request))
            chunks = collect_async_iter(response.body_iterator)

        body = "".join(chunks)
        self.assertIn('"content": "answer"', body)
        self.assertIn("data: [DONE]", body)
        self.assertEqual(self.pool.released[-1], self.session)

    def test_new_chat_never_reuses_old_conversation_id(self):
        self.session.prepare_chat = AsyncMock(return_value=None)
        request = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "fresh"}],
            new_chat=True,
            conversation_id=CHAT_A,
        )
        http_request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1")
        )
        with (
            patch.object(main.settings, "DISABLE_RATE_LIMIT", True),
            patch.object(main.settings, "MEMORY_MODE", "server"),
            patch.object(main.settings, "get_initial_message", return_value=""),
        ):
            response = run_inline(main.chat_completions(request, http_request))

        self.assertEqual(response.choices[0].message.content, "answer")
        self.session.prepare_chat.assert_awaited_once_with(
            initial_message="",
            initial_deep_think=False,
            initial_search=False,
            create_new=True,
            chat_id=None,
        )
        self.assertIsNone(self.session.last_stream_kwargs["chat_id"])

    def test_busy_retry_uses_unified_source_without_name_error(self):
        self.session.stream_responses = ["сервис занят", "answer"]
        request = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "hello"}],
        )
        http_request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1")
        )
        with (
            patch.object(main.settings, "DISABLE_RATE_LIMIT", True),
            patch.object(main.settings, "MEMORY_MODE", "server"),
            patch.object(main.settings, "RETRY_MAX_ATTEMPTS", 1),
            patch.object(main.settings, "get_initial_message", return_value="boot"),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            response = run_inline(main.chat_completions(request, http_request))

        self.assertEqual(response.choices[0].message.content, "answer")
        self.assertEqual(self.session.prepare_chat.await_count, 2)
        self.session.prepare_chat.assert_any_await(
            initial_message="boot",
            initial_deep_think=False,
            initial_search=False,
            create_new=True,
            chat_id=None,
        )

    def test_initial_message_uses_its_own_model(self):
        with (
            patch.object(main.settings, "get_initial_message", return_value="boot"),
            patch.object(main.settings, "INITIAL_MESSAGE_MODEL", "deepseek-think-search"),
        ):
            message, deep_think, search = main._get_initial_message_config()

        self.assertEqual(message, "boot")
        self.assertTrue(deep_think)
        self.assertTrue(search)


if __name__ == "__main__":
    unittest.main()
