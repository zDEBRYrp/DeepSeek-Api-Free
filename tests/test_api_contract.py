import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import main, model_registry
from app.schemas import ChatCompletionRequest, ChatSwitchRequest
from app.security import SlidingWindowLimiter


class ApiContractTests(unittest.TestCase):
    def test_all_advertised_models_resolve(self):
        models = model_registry.list_models()
        self.assertGreaterEqual(len(models), 4)
        for model in models:
            self.assertTrue(model_registry.is_supported_model(model["id"]))
            toggles = model_registry.resolve_toggles(model["id"])
            self.assertEqual(len(toggles), 2)

    def test_chat_ids_reject_paths_and_urls(self):
        invalid = [
            "../healthz", "https://example.com", "abc/def", "short", "--------"
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChatSwitchRequest(chat_id=value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChatCompletionRequest(
                    messages=[{"role": "user", "content": "hi"}],
                    conversation_id=value,
                )

    def test_rate_limiter_rejects_after_limit(self):
        limiter = SlidingWindowLimiter(limit_per_minute=2, window_seconds=60)
        self.assertTrue(limiter.check("127.0.0.1")[0])
        self.assertTrue(limiter.check("127.0.0.1")[0])
        allowed, retry_after = limiter.check("127.0.0.1")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_server_continuation_requires_conversation_id(self):
        request = ChatCompletionRequest(
            messages=[
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]
        )
        with (
            patch.object(main.settings, "MEMORY_MODE", "server"),
            patch.object(main.settings, "STRICT_CHAT_AFFINITY", True),
            self.assertRaises(HTTPException) as raised,
        ):
            main._validate_chat_affinity(request)
        self.assertEqual(raised.exception.status_code, 400)

        request.conversation_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        main._validate_chat_affinity(request)


if __name__ == "__main__":
    unittest.main()
