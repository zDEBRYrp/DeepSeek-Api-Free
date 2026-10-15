import unittest
from unittest.mock import AsyncMock, patch

from app.browser_session import BrowserSession, BrowserSessionError
from tests.helpers import run_inline


class BrowserToggleStateTests(unittest.TestCase):
    def test_reads_pressed_state_from_parent(self):
        session = BrowserSession()
        toggle = AsyncMock()
        toggle.evaluate = AsyncMock(
            return_value=[
                {"tag": "div", "className": "ds-toggle-button", "attributes": {}},
                {"tag": "button", "className": "mode-control", "attributes": {"aria-pressed": "true"}},
            ]
        )

        state = run_inline(session._read_toggle_state(toggle))

        self.assertTrue(state)

    def test_reads_active_data_state_from_descendant(self):
        session = BrowserSession()
        toggle = AsyncMock()
        toggle.evaluate = AsyncMock(
            return_value=[
                {"tag": "div", "className": "ds-toggle-button", "attributes": {}},
                {"tag": "span", "className": "indicator active", "attributes": {"data-state": "on"}},
            ]
        )

        state = run_inline(session._read_toggle_state(toggle))

        self.assertTrue(state)

    def test_explicit_false_state_wins_over_base_class(self):
        session = BrowserSession()
        toggle = AsyncMock()
        toggle.evaluate = AsyncMock(
            return_value=[
                {
                    "tag": "button",
                    "className": "ds-toggle-button",
                    "attributes": {"aria-pressed": "false"},
                }
            ]
        )

        state = run_inline(session._read_toggle_state(toggle))

        self.assertFalse(state)


class BrowserToggleActionTests(unittest.TestCase):
    def _session_with_toggle(self):
        session = BrowserSession()
        session._find_toggle = AsyncMock()
        toggle = AsyncMock()
        session._find_toggle.return_value = toggle
        return session, toggle

    def test_clicks_once_and_confirms_requested_state(self):
        session, toggle = self._session_with_toggle()
        session._read_toggle_state = AsyncMock(side_effect=[False, True])

        with patch("app.browser_session.asyncio.sleep", new=AsyncMock()):
            run_inline(session._toggle_button("", True, ["DeepThink"]))

        toggle.click.assert_awaited_once()

    def test_does_not_click_when_state_is_already_correct(self):
        session, toggle = self._session_with_toggle()
        session._read_toggle_state = AsyncMock(return_value=True)

        run_inline(session._toggle_button("", True, ["DeepThink"]))

        toggle.click.assert_not_awaited()

    def test_never_retries_click_when_state_cannot_be_confirmed(self):
        session, toggle = self._session_with_toggle()
        session._read_toggle_state = AsyncMock(side_effect=[False] + [None] * 6)

        with patch("app.browser_session.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BrowserSessionError):
                run_inline(session._toggle_button("", True, ["DeepThink"]))

        toggle.click.assert_awaited_once()

    def test_missing_toggle_aborts_instead_of_sending_wrong_mode(self):
        session = BrowserSession()
        session._find_toggle = AsyncMock(return_value=None)

        with self.assertRaises(BrowserSessionError):
            run_inline(session._toggle_button("", True, ["DeepThink"]))


if __name__ == "__main__":
    unittest.main()
