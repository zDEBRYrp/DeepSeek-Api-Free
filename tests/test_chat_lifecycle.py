import unittest
from unittest.mock import AsyncMock

from app.browser_session import BrowserSession
from app.session_pool import SessionPool
from tests.helpers import run_inline


CHAT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class ChatInitializationTests(unittest.TestCase):
    def test_bootstrap_creates_and_verifies_chat(self):
        session = BrowserSession()
        session._new_chat_unlocked = AsyncMock(return_value="https://chat.deepseek.com")
        session.get_current_chat_id = AsyncMock(
            side_effect=[None, CHAT_A, CHAT_A]
        )
        session._submit_message = AsyncMock(return_value="")
        session._wait_response_complete = AsyncMock(return_value=("", "ok"))

        result = run_inline(
            session.prepare_chat(
                initial_message="bootstrap",
                initial_deep_think=True,
                initial_search=False,
                create_new=True,
            )
        )

        self.assertEqual(result, CHAT_A)
        session._submit_message.assert_awaited_once_with(
            "bootstrap", deep_think=True, search=False, chat_id=None
        )
        session._wait_response_complete.assert_awaited_once()

    def test_existing_chat_is_not_bootstrapped_again(self):
        session = BrowserSession()
        session.ensure_chat = AsyncMock()
        session.get_current_chat_id = AsyncMock(return_value=CHAT_A)
        session._submit_message = AsyncMock()

        result = run_inline(
            session.prepare_chat(initial_message="bootstrap", chat_id=CHAT_A)
        )

        self.assertEqual(result, CHAT_A)
        session.ensure_chat.assert_awaited_once_with(CHAT_A)
        session._submit_message.assert_not_awaited()

    def test_disabled_bootstrap_still_creates_new_composer(self):
        session = BrowserSession()
        session._new_chat_unlocked = AsyncMock(return_value="https://chat.deepseek.com")
        session.get_current_chat_id = AsyncMock(return_value=None)
        session._submit_message = AsyncMock()

        result = run_inline(
            session.prepare_chat(initial_message="", create_new=True)
        )

        self.assertIsNone(result)
        session._new_chat_unlocked.assert_awaited_once()
        session._submit_message.assert_not_awaited()


class SessionAffinityTests(unittest.TestCase):
    def test_conversation_returns_to_its_original_session(self):
        first = object()
        second = object()
        pool = SessionPool(max_wait_seconds=0.1)
        pool.add("first", first)
        pool.add("second", second)

        acquired = run_inline(pool.acquire())
        self.assertIs(acquired, first)
        run_inline(pool.bind_chat(first, CHAT_A))
        run_inline(pool.release(first))

        # Round-robin без affinity выбрал бы second.
        resumed = run_inline(pool.acquire(chat_id=CHAT_A))
        self.assertIs(resumed, first)
        run_inline(pool.release(resumed))

    def test_unknown_chat_uses_round_robin(self):
        first = object()
        second = object()
        pool = SessionPool(max_wait_seconds=0.1)
        pool.add("first", first)
        pool.add("second", second)

        acquired = run_inline(
            pool.acquire(chat_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        )
        self.assertIs(acquired, first)
        run_inline(pool.release(acquired))


if __name__ == "__main__":
    unittest.main()
